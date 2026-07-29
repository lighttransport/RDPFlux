from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote

from .client import ControlError

# Presents the same surface MCPServer expects (request / screenshot / read_file /
# write_file) but backed by the client's REST listener instead of a mux peer.
# This is how a standalone MCP subprocess — the shape Claude Desktop launches —
# reaches a tunnel whose mux lives inside the mstsc plugin process.


class HTTPControlClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 30.0) -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    async def request(self, op: str, params: dict[str, Any] | None = None) -> tuple[dict[str, Any], bytes]:
        params = params or {}
        if op == "screenshot":
            status, ctype, body = await self._send("POST", "/v1/screenshot", json_body=params)
            self._raise_for_status(status, ctype, body)
            fmt = "jpeg" if "jpeg" in ctype else "png"
            return {"format": fmt}, body
        if op == "action":
            return await self._json("POST", "/v1/action", json_body=params)
        if op == "exec":
            return await self._json("POST", "/v1/exec", json_body=params)
        if op == "list_dir":
            return await self._json("GET", f"/v1/dir?path={quote(params.get('path', '.'))}")
        raise ControlError(f"HTTP control client does not support op {op!r}")

    async def screenshot(self, *, width: int | None = None, image_format: str = "png",
                         quality: int = 80) -> tuple[dict[str, Any], bytes]:
        params: dict[str, Any] = {"format": image_format, "quality": quality}
        if width is not None:
            params["width"] = width
        return await self.request("screenshot", params)

    async def read_file(self, path: str) -> tuple[dict[str, Any], bytes]:
        status, ctype, body = await self._send("GET", f"/v1/file?path={quote(path)}")
        self._raise_for_status(status, ctype, body)
        return {}, body

    async def write_file(self, path: str, data: bytes, *, create_parents: bool = False) -> dict[str, Any]:
        query = f"/v1/file?path={quote(path)}"
        if create_parents:
            query += "&create_parents=1"
        status, ctype, body = await self._send("PUT", query, body=data,
                                               ctype="application/octet-stream")
        self._raise_for_status(status, ctype, body)
        return json.loads(body) if body else {}

    async def _json(self, method: str, path: str, *, json_body=None) -> tuple[dict[str, Any], bytes]:
        status, ctype, body = await self._send(method, path, json_body=json_body)
        self._raise_for_status(status, ctype, body)
        return (json.loads(body) if body else {}), b""

    def _raise_for_status(self, status: int, ctype: str, body: bytes) -> None:
        if status == 200:
            return
        message = body.decode("utf-8", "replace")
        if "json" in ctype:
            try:
                message = json.loads(body).get("error", message)
            except json.JSONDecodeError:
                pass
        raise ControlError(f"HTTP {status}: {message}")

    async def _send(self, method: str, path: str, *, json_body=None, body: bytes = b"",
                    ctype: str = "application/json") -> tuple[int, str, bytes]:
        host, port = _split_base(self.base)
        payload = json.dumps(json_body).encode() if json_body is not None else body
        headers = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: close"]
        if self.token:
            headers.append(f"Authorization: Bearer {self.token}")
        if payload:
            headers.append(f"Content-Type: {ctype}")
            headers.append(f"Content-Length: {len(payload)}")
        request = ("\r\n".join(headers) + "\r\n\r\n").encode("latin-1") + payload

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), self.timeout)
        try:
            writer.write(request)
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(), self.timeout)
        finally:
            writer.close()
        head, _, resp_body = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ")[1])
        resp_ctype = ""
        for line in lines[1:]:
            if line.lower().startswith("content-type:"):
                resp_ctype = line.partition(":")[2].strip()
        return status, resp_ctype, resp_body


def _split_base(base: str) -> tuple[str, int]:
    without_scheme = base.split("://", 1)[-1]
    host, _, port = without_scheme.partition(":")
    return host or "127.0.0.1", int(port or "80")
