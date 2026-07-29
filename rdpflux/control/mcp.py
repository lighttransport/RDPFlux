from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from typing import Any

from .actions import ACTIONS, MAX_SCROLL_AMOUNT, SCROLL_DIRECTIONS
from .client import ControlClient, ControlError

LOG = logging.getLogger(__name__)

# A minimal MCP stdio server (JSON-RPC 2.0 over line-delimited stdin/stdout) so
# Claude Code and Claude Desktop can drive the tunnel with no third-party SDK.
# It shares actions.py with the REST surface, so the two never diverge.
PROTOCOL_VERSION = "2024-11-05"


def _coordinate_prop(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "integer", "minimum": 0},
            "minItems": 2, "maxItems": 2, "description": description}


def tool_definitions(*, exec_enabled: bool, files_enabled: bool) -> list[dict[str, Any]]:
    tools = [
        {
            "name": "screenshot",
            "description": "Capture the remote desktop and return it as an image.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "width": {"type": "integer", "minimum": 320, "maximum": 2576,
                               "description": "Delivered width; height follows the display aspect ratio."},
                    "format": {"type": "string", "enum": ["png", "jpeg"], "default": "png"},
                },
            },
        },
        {
            "name": "computer_action",
            "description": "Perform one mouse or keyboard action. Coordinates are in the "
                           "pixel space of the most recent screenshot.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(ACTIONS),
                                "description": "; ".join(f"{s.name}: {s.description}" for s in ACTIONS.values())},
                    "coordinate": _coordinate_prop("Target point [x, y]."),
                    "start_coordinate": _coordinate_prop("Drag origin [x, y]."),
                    "text": {"type": "string", "description": "Literal text, or a key/chord like 'ctrl+s'."},
                    "duration": {"type": "number", "minimum": 0, "description": "Seconds."},
                    "scroll_direction": {"type": "string", "enum": list(SCROLL_DIRECTIONS)},
                    "scroll_amount": {"type": "integer", "minimum": 1, "maximum": MAX_SCROLL_AMOUNT},
                },
                "required": ["action"],
            },
        },
    ]
    if exec_enabled:
        tools.append({
            "name": "run_command",
            "description": "Run a command on the remote host and return its output.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {"type": "array", "items": {"type": "string"},
                                 "description": "argv list; not passed through a shell."},
                    "timeout": {"type": "number", "minimum": 0, "maximum": 300},
                    "cwd": {"type": "string"},
                },
                "required": ["command"],
            },
        })
    if files_enabled:
        tools.append({
            "name": "read_file",
            "description": "Read a text file from the remote host.",
            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                             "required": ["path"]},
        })
        tools.append({
            "name": "write_file",
            "description": "Write a text file on the remote host.",
            "inputSchema": {"type": "object",
                             "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                             "required": ["path", "content"]},
        })
        tools.append({
            "name": "list_dir",
            "description": "List a directory on the remote host.",
            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
        })
    return tools


class MCPServer:
    def __init__(self, control: ControlClient, *, exec_enabled: bool = False,
                 files_enabled: bool = False) -> None:
        self.control = control
        self.tools = tool_definitions(exec_enabled=exec_enabled, files_enabled=files_enabled)

    async def dispatch(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        try:
            result = await self._method(method, request.get("params") or {})
        except ControlError as exc:
            return _error(request_id, -32000, str(exc))
        except _MethodError as exc:
            return _error(request_id, exc.code, exc.message)
        except Exception as exc:  # pragma: no cover - defensive
            LOG.exception("MCP method failed")
            return _error(request_id, -32603, str(exc))
        if request_id is None:
            return None  # a notification expects no reply
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    async def _method(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "rdpflux-control", "version": "1"},
            }
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "tools/list":
            return {"tools": self.tools}
        if method == "tools/call":
            return await self._call(params.get("name"), params.get("arguments") or {})
        raise _MethodError(-32601, f"unknown method {method}")

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "screenshot":
            result, body = await self.control.screenshot(
                width=arguments.get("width"), image_format=arguments.get("format", "png"))
            mime = "image/jpeg" if result.get("format") == "jpeg" else "image/png"
            return {"content": [{"type": "image", "data": base64.b64encode(body).decode(),
                                 "mimeType": mime}]}
        if name == "computer_action":
            result, _ = await self.control.request("action", arguments)
            return _text_result(result)
        if name == "run_command":
            result, _ = await self.control.request("exec", arguments)
            return _text_result(result)
        if name == "read_file":
            _, data = await self.control.read_file(arguments["path"])
            return _text_result(data.decode("utf-8", "replace"))
        if name == "write_file":
            result = await self.control.write_file(
                arguments["path"], arguments["content"].encode("utf-8"), create_parents=True)
            return _text_result(result)
        if name == "list_dir":
            result, _ = await self.control.request("list_dir", {"path": arguments.get("path", ".")})
            return _text_result(result)
        raise _MethodError(-32602, f"unknown tool {name}")

    async def serve_stdio(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        while True:
            line = await reader.readline()
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                _emit(_error(None, -32700, "parse error"))
                continue
            response = await self.dispatch(request)
            if response is not None:
                _emit(response)


class _MethodError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _text_result(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, indent=2)
    return {"content": [{"type": "text", "text": text}]}


def _emit(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()
