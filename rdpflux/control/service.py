from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from ..mux import MuxStream, describe_exception
from . import execute
from .actions import Action, ActionError, parse_action, scale_action
from .framing import MessageReader, encode_message

LOG = logging.getLogger(__name__)

DEFAULT_WIDTH = 1920
MIN_WIDTH = 320
# Anthropic's high-resolution tier caps at 2576px on the long edge; anything
# larger is downscaled before the model sees it, so sending more only costs
# tunnel bandwidth.
MAX_WIDTH = 2576
FORMATS = ("png", "jpeg")


@dataclass(frozen=True, slots=True)
class Screenshot:
    data: bytes
    width: int
    height: int
    native_width: int
    native_height: int
    format: str


class ControlBackend(Protocol):
    """Platform side of the control service. Implemented per OS."""

    def native_size(self) -> tuple[int, int]:
        """Current display size in physical pixels."""
        ...

    async def screenshot(self, *, width: int, image_format: str, quality: int) -> Screenshot:
        ...

    async def perform(self, action: Action) -> dict[str, Any]:
        ...


class ControlService:
    """Agent-side dispatcher for one mux connection.

    One request per stream, matching how bridge_socket scopes a stream to one
    connection. Screenshot geometry is remembered across streams so actions can
    be expressed in the delivered image's coordinate space.
    """

    def __init__(self, backend: ControlBackend, *, allow_exec: bool = False,
                 files: Any = None) -> None:
        self.backend = backend
        self.allow_exec = allow_exec
        self.files = files
        self._delivered: tuple[int, int] | None = None

    async def handle(self, stream: MuxStream) -> None:
        try:
            message = await MessageReader(stream).read_message()
            if message is None:
                return
            header, body = message
            try:
                response, payload = await self._dispatch(header, body)
            except ActionError as exc:
                response, payload = {"ok": False, "error": str(exc)}, b""
            except Exception as exc:
                LOG.exception("control request failed")
                response, payload = {"ok": False, "error": describe_exception(exc)}, b""
            await stream.write(encode_message(response, payload))
            await stream.write_eof()
        except Exception as exc:
            LOG.warning("control stream failed: %s", describe_exception(exc))
        finally:
            await stream.close()

    async def _dispatch(self, header: dict[str, Any], body: bytes) -> tuple[dict[str, Any], bytes]:
        op = header.get("op")
        params = header.get("params") or {}
        if not isinstance(params, dict):
            raise ActionError("params must be an object")
        if op == "screenshot":
            return await self._screenshot(params)
        if op == "action":
            return await self._action(params)
        if op == "exec":
            if not self.allow_exec:
                raise ActionError("command execution is disabled")
            return {"ok": True, "result": await execute.run(params)}, b""
        if op in ("read_file", "write_file", "list_dir"):
            if self.files is None:
                raise ActionError("file transfer is disabled")
            return self._files(op, params, body)
        raise ActionError(f"unknown op {op!r}")

    def _files(self, op: str, params: dict[str, Any], body: bytes) -> tuple[dict[str, Any], bytes]:
        if op == "read_file":
            result, data = self.files.read(params.get("path"))
            return {"ok": True, "result": result}, data
        if op == "write_file":
            result = self.files.write(params.get("path"), body,
                                      create_parents=bool(params.get("create_parents")))
            return {"ok": True, "result": result}, b""
        return {"ok": True, "result": self.files.list(params.get("path") or ".")}, b""

    async def _screenshot(self, params: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        width = params.get("width", DEFAULT_WIDTH)
        if isinstance(width, bool) or not isinstance(width, int):
            raise ActionError("width must be an integer")
        if not MIN_WIDTH <= width <= MAX_WIDTH:
            raise ActionError(f"width must be between {MIN_WIDTH} and {MAX_WIDTH}")

        image_format = params.get("format", "png")
        if image_format not in FORMATS:
            raise ActionError(f"format must be one of {', '.join(FORMATS)}")

        quality = params.get("quality", 80)
        if isinstance(quality, bool) or not isinstance(quality, int) or not 1 <= quality <= 100:
            raise ActionError("quality must be an integer between 1 and 100")

        shot = await self.backend.screenshot(
            width=width, image_format=image_format, quality=quality,
        )
        self._delivered = (shot.width, shot.height)
        return {
            "ok": True,
            "result": {
                "width": shot.width,
                "height": shot.height,
                "native": [shot.native_width, shot.native_height],
                "format": shot.format,
            },
        }, shot.data

    async def _action(self, params: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        action = parse_action(params)
        scale_x, scale_y = self._scale()
        result = await self.backend.perform(scale_action(action, scale_x, scale_y))
        result = dict(result or {})
        # cursor_position comes back in native pixels; return it in the same space
        # the caller asked in, so one request never mixes two coordinate systems.
        if "coordinate" in result and (scale_x, scale_y) != (1.0, 1.0):
            x, y = result["coordinate"]
            result["coordinate"] = [round(x / scale_x), round(y / scale_y)]
        return {"ok": True, "result": result}, b""

    def _scale(self) -> tuple[float, float]:
        """Screenshot-space to native-pixel scale factors."""
        if self._delivered is None:
            return 1.0, 1.0
        delivered_width, delivered_height = self._delivered
        if not delivered_width or not delivered_height:
            return 1.0, 1.0
        # Read native size at action time rather than reusing the value captured
        # with the screenshot, so a resolution change is picked up immediately.
        native_width, native_height = self.backend.native_size()
        return native_width / delivered_width, native_height / delivered_height
