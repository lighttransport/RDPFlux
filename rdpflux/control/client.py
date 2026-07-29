from __future__ import annotations

from typing import Any

from ..mux import MuxPeer
from .framing import FramingError, MessageReader, encode_message


class ControlError(Exception):
    """The agent rejected a control request."""


class ControlClient:
    """Client half of the control service.

    Each request opens its own mux stream, mirroring how a forwarded connection
    maps to one stream. That costs a round trip per request; if measurements show
    that dominating click latency, switch to one persistent stream with request
    IDs rather than guessing now.
    """

    def __init__(self, peer: MuxPeer, timeout: float = 30.0) -> None:
        self.peer = peer
        self.timeout = timeout

    async def request(self, op: str, params: dict[str, Any] | None = None) -> tuple[dict[str, Any], bytes]:
        stream = await self.peer.open_stream({"kind": "control"}, self.timeout)
        try:
            await stream.write(encode_message({"op": op, "params": params or {}}))
            await stream.write_eof()
            message = await MessageReader(stream).read_message()
            if message is None:
                raise ControlError("agent closed the control stream without replying")
            header, body = message
            if not header.get("ok"):
                raise ControlError(str(header.get("error", "control request rejected")))
            return header.get("result") or {}, body
        except FramingError as exc:
            raise ControlError(str(exc)) from exc
        finally:
            await stream.close()

    async def screenshot(self, *, width: int | None = None, image_format: str = "png",
                         quality: int = 80) -> tuple[dict[str, Any], bytes]:
        params: dict[str, Any] = {"format": image_format, "quality": quality}
        if width is not None:
            params["width"] = width
        return await self.request("screenshot", params)

    async def act(self, action: str, **params: Any) -> dict[str, Any]:
        result, _ = await self.request("action", {"action": action, **params})
        return result

    async def exec(self, command: list[str], *, timeout: float | None = None,
                   cwd: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"command": command}
        if timeout is not None:
            params["timeout"] = timeout
        if cwd is not None:
            params["cwd"] = cwd
        result, _ = await self.request("exec", params)
        return result

    async def read_file(self, path: str) -> tuple[dict[str, Any], bytes]:
        return await self.request("read_file", {"path": path})

    async def write_file(self, path: str, data: bytes, *, create_parents: bool = False) -> dict[str, Any]:
        stream = await self.peer.open_stream({"kind": "control"}, self.timeout)
        try:
            await stream.write(encode_message(
                {"op": "write_file", "params": {"path": path, "create_parents": create_parents}},
                data,
            ))
            await stream.write_eof()
            message = await MessageReader(stream).read_message()
            if message is None:
                raise ControlError("agent closed the control stream without replying")
            header, _ = message
            if not header.get("ok"):
                raise ControlError(str(header.get("error", "write rejected")))
            return header.get("result") or {}
        finally:
            await stream.close()

    async def list_dir(self, path: str = ".") -> dict[str, Any]:
        result, _ = await self.request("list_dir", {"path": path})
        return result
