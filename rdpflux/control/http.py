from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .client import ControlClient, ControlError
from .openapi import build_spec

LOG = logging.getLogger(__name__)

MAX_REQUEST_BODY = 64 * 1024 * 1024
MAX_HEADERS = 64 * 1024
_STATUS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
           404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large",
           500: "Internal Server Error", 502: "Bad Gateway"}


class ControlHTTPServer:
    """Loopback REST facade over a ControlClient.

    Runs on the client's event loop and shares its mux peer, so there is no extra
    process hop between the LLM and the tunnel. A bearer token guards it because
    a loopback listener is reachable by any local process.
    """

    def __init__(self, control: ControlClient, *, token: str,
                 exec_enabled: bool = False, files_enabled: bool = False,
                 system_enabled: bool = False, clipboard_enabled: bool = False) -> None:
        self.control = control
        self.token = token
        self._spec = build_spec(exec_enabled=exec_enabled, files_enabled=files_enabled,
                                system_enabled=system_enabled, clipboard_enabled=clipboard_enabled)
        self._server: asyncio.AbstractServer | None = None
        self._shells: dict[str, Any] = {}

    async def start(self, host: str, port: int) -> asyncio.AbstractServer:
        self._server = await asyncio.start_server(self._handle, host, port)
        return self._server

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib_suppress():
                await self._server.wait_closed()
        await asyncio.gather(*(shell.close() for shell in self._shells.values()),
                             return_exceptions=True)
        self._shells.clear()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await _read_request(reader)
            if request is None:
                return
            method, path, headers, body = request
            status, ctype, payload = await self._route(method, path, headers, body)
        except _HTTPError as exc:
            status, ctype, payload = exc.status, "application/json", _json_error(exc.message)
        except Exception as exc:
            LOG.exception("control HTTP handler failed")
            status, ctype, payload = 500, "application/json", _json_error(str(exc))
        else:
            pass
        try:
            await _write_response(writer, status, ctype, payload)
        finally:
            writer.close()
            with contextlib_suppress():
                await writer.wait_closed()

    async def _route(self, method: str, path: str, headers: dict[str, str],
                     body: bytes) -> tuple[int, str, bytes]:
        parsed = urlsplit(path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route == "/openapi.json" and method == "GET":
            return 200, "application/json", _dumps(self._spec)
        if route in ("/health", "/") and method == "GET":
            return 200, "application/json", _dumps({"ok": True})

        self._authorize(headers)

        if route == "/v1/screenshot" and method == "POST":
            return await self._screenshot(_json_body(body))
        if route == "/v1/action" and method == "POST":
            return await self._forward("action", _json_body(body))
        if route == "/v1/exec" and method == "POST":
            return await self._forward("exec", _json_body(body))
        if route == "/v1/dir" and method == "GET":
            return await self._forward("list_dir", {"path": _one(query, "path", ".")})
        if route == "/v1/system/processes" and method == "GET":
            return await self._forward("system_process_list", {})
        if route == "/v1/system/processes/terminate" and method == "POST":
            return await self._forward("system_process_terminate", _json_body(body))
        if route == "/v1/system/services" and method == "GET":
            return await self._forward("system_service_list", {"name": _one(query, "name", "")})
        if route == "/v1/system/services/control" and method == "POST":
            return await self._forward("system_service_control", _json_body(body))
        if route == "/v1/system/tasks" and method == "GET":
            return await self._forward("system_task_list", {"name": _one(query, "name", "")})
        if route == "/v1/system/tasks/run" and method == "POST":
            return await self._forward("system_task_run", _json_body(body))
        if route == "/v1/system/diagnostics" and method == "GET":
            return await self._forward("system_diagnostics", {})
        if route == "/v1/clipboard" and method == "GET":
            return await self._forward("clipboard_read", {})
        if route == "/v1/clipboard" and method == "PUT":
            return await self._forward("clipboard_write", _json_body(body))
        if route == "/v1/sessions" and method == "POST":
            return await self._session_open(_json_body(body))
        if route.startswith("/v1/sessions/"):
            return await self._session_route(method, route, body)
        if route == "/v1/file":
            return await self._file(method, query, body)
        raise _HTTPError(404, f"no route for {method} {route}")

    async def _session_open(self, params: dict[str, Any]) -> tuple[int, str, bytes]:
        try:
            shell = await self.control.open_shell(
                program=params.get("program", "powershell"), cwd=params.get("cwd"))
        except ControlError as exc:
            raise _HTTPError(502, str(exc)) from exc
        session_id = secrets.token_urlsafe(18)
        self._shells[session_id] = shell
        return 200, "application/json", _dumps({"session": session_id})

    async def _session_route(self, method: str, route: str, body: bytes) -> tuple[int, str, bytes]:
        parts = route.split("/")
        if len(parts) < 5 or not parts[3]:
            raise _HTTPError(404, "invalid session path")
        session = self._shells.get(parts[3])
        if session is None:
            raise _HTTPError(404, "no such session")
        operation = parts[4]
        if method == "POST" and operation == "run":
            params = _json_body(body)
            command = params.get("command")
            if not isinstance(command, str):
                raise _HTTPError(400, "command must be a string")
            chunks: list[str] = []
            try:
                code = await session.run(command, chunks.append)
            except ControlError as exc:
                raise _HTTPError(502, str(exc)) from exc
            return 200, "application/json", _dumps({"exit_code": code, "stdout": "".join(chunks)})
        if method == "POST" and operation == "interrupt":
            await session.interrupt()
            return 200, "application/json", _dumps({"interrupted": True})
        if method == "DELETE" and operation == "close":
            await session.close()
            self._shells.pop(parts[3], None)
            return 200, "application/json", _dumps({"closed": True})
        raise _HTTPError(405, f"{method} not allowed on session")

    def _authorize(self, headers: dict[str, str]) -> None:
        if not self.token:
            return
        header = headers.get("authorization", "")
        prefix = "bearer "
        supplied = header[len(prefix):] if header.lower().startswith(prefix) else ""
        if not supplied or not secrets.compare_digest(supplied, self.token):
            raise _HTTPError(401, "missing or invalid bearer token")

    async def _screenshot(self, params: dict[str, Any]) -> tuple[int, str, bytes]:
        try:
            result, body = await self.control.request("screenshot", params)
        except ControlError as exc:
            raise _HTTPError(502, str(exc)) from exc
        ctype = "image/jpeg" if result.get("format") == "jpeg" else "image/png"
        return 200, ctype, body

    async def _forward(self, op: str, params: dict[str, Any]) -> tuple[int, str, bytes]:
        try:
            result, _ = await self.control.request(op, params)
        except ControlError as exc:
            raise _HTTPError(502, str(exc)) from exc
        return 200, "application/json", _dumps(result)

    async def _file(self, method: str, query: dict, body: bytes) -> tuple[int, str, bytes]:
        path = _one(query, "path")
        if not path:
            raise _HTTPError(400, "path query parameter is required")
        try:
            if method == "GET":
                _, data = await self.control.read_file(path)
                return 200, "application/octet-stream", data
            if method == "PUT":
                create = _one(query, "create_parents", "").lower() in ("1", "true", "yes")
                result = await self.control.write_file(path, body, create_parents=create)
                return 200, "application/json", _dumps(result)
        except ControlError as exc:
            raise _HTTPError(502, str(exc)) from exc
        raise _HTTPError(405, f"{method} not allowed on /v1/file")


class _HTTPError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def contextlib_suppress():
    import contextlib
    return contextlib.suppress(Exception)


def _dumps(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _json_error(message: str) -> bytes:
    return _dumps({"error": message})


def _one(query: dict, key: str, default: str = "") -> str:
    values = query.get(key)
    return values[0] if values else default


def _json_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _HTTPError(400, f"invalid JSON body: {exc}") from exc
    if not isinstance(value, dict):
        raise _HTTPError(400, "request body must be a JSON object")
    return value


async def _read_request(reader: asyncio.StreamReader):
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return None
    if len(head) > MAX_HEADERS:
        raise _HTTPError(413, "request headers too large")
    lines = head.decode("latin-1").split("\r\n")
    method, path, _ = (lines[0].split(" ", 2) + ["", ""])[:3]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()

    body = b""
    length = int(headers.get("content-length", "0") or "0")
    if length > MAX_REQUEST_BODY:
        raise _HTTPError(413, "request body too large")
    if length:
        try:
            body = await reader.readexactly(length)
        except asyncio.IncompleteReadError:
            raise _HTTPError(400, "request body shorter than Content-Length")
    return method.upper(), path, headers, body


async def _write_response(writer: asyncio.StreamWriter, status: int, ctype: str, body: bytes) -> None:
    reason = _STATUS.get(status, "OK")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {ctype}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("latin-1")
    writer.write(head + body)
    await writer.drain()
