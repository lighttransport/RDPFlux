from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from ..mux import MuxStream, describe_exception
from . import execute
from . import system
from . import clipboard
from .actions import Action, ActionError, parse_action, scale_action
from .framing import MessageReader, encode_message
from .shell import PersistentShell

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
                 files: Any = None, system_enabled: bool = False,
                 allow_process_terminate: bool = False,
                 service_allowlist: list[str] | None = None,
                 task_allowlist: list[str] | None = None,
                 clipboard_enabled: bool = False) -> None:
        self.backend = backend
        self.allow_exec = allow_exec
        self.files = files
        self.system_enabled = system_enabled
        self.allow_process_terminate = allow_process_terminate
        self.service_allowlist = {value.casefold() for value in (service_allowlist or [])}
        self.task_allowlist = {value.casefold() for value in (task_allowlist or [])}
        self.clipboard_enabled = clipboard_enabled
        self._delivered: tuple[int, int] | None = None

    async def handle(self, stream: MuxStream) -> None:
        try:
            message = await MessageReader(stream).read_message()
            if message is None:
                return
            header, body = message
            if header.get("op") == "shell_open":
                await self._handle_shell(stream, header.get("params") or {})
                return
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

    async def _handle_shell(self, stream: MuxStream, params: dict[str, Any]) -> None:
        if not self.allow_exec:
            await stream.write(encode_message({"ok": False, "error": "command execution is disabled"}))
            await stream.write_eof()
            return
        if not isinstance(params, dict):
            await stream.write(encode_message({"ok": False, "error": "params must be an object"}))
            await stream.write_eof()
            return
        shell = PersistentShell(params.get("program", "powershell"), params.get("cwd"))
        await stream.write(encode_message({"ok": True, "kind": "ready"}))
        reader = MessageReader(stream)
        read_task = asyncio.create_task(reader.read_message())
        run_task: asyncio.Task[int | None] | None = None

        async def output(text: str) -> None:
            await stream.write(encode_message({"ok": True, "kind": "stdout"}, text.encode("utf-8")))

        try:
            while True:
                watched: set[asyncio.Task[Any]] = {read_task}
                if run_task is not None:
                    watched.add(run_task)
                done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
                if run_task is not None and run_task in done:
                    try:
                        code = run_task.result()
                        await stream.write(encode_message({"ok": True, "kind": "result",
                                                           "exit_code": code}))
                    except Exception as exc:
                        await stream.write(encode_message({"ok": False, "kind": "error",
                                                           "error": describe_exception(exc)}))
                    run_task = None
                if read_task in done:
                    message = read_task.result()
                    if message is None:
                        break
                    header, _body = message
                    read_task = asyncio.create_task(reader.read_message())
                    op = header.get("op")
                    if op == "input":
                        if run_task is not None:
                            await stream.write(encode_message({"ok": False, "kind": "error",
                                                               "error": "shell is busy"}))
                            continue
                        command = (header.get("params") or {}).get("command")
                        if not isinstance(command, str):
                            await stream.write(encode_message({"ok": False, "kind": "error",
                                                               "error": "command must be a string"}))
                            continue
                        run_task = asyncio.create_task(shell.run(command, output))
                    elif op == "interrupt":
                        await shell.interrupt()
                    elif op == "close":
                        break
                    else:
                        await stream.write(encode_message({"ok": False, "kind": "error",
                                                           "error": "unknown shell operation"}))
        finally:
            read_task.cancel()
            if run_task is not None:
                run_task.cancel()
            await asyncio.gather(read_task, *( [run_task] if run_task else [] ),
                                 return_exceptions=True)
            await shell.close()

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
        if op.startswith("system_"):
            if not self.system_enabled:
                raise ActionError("system operations are disabled")
            return {"ok": True, "result": await self._system(op, params)}, b""
        if op == "clipboard_read":
            if not self.clipboard_enabled:
                raise ActionError("clipboard control is disabled")
            return {"ok": True, "result": await clipboard.read_text()}, b""
        if op == "clipboard_write":
            if not self.clipboard_enabled:
                raise ActionError("clipboard control is disabled")
            return {"ok": True, "result": await clipboard.write_text(params.get("text"))}, b""
        if op in ("read_file", "write_file", "list_dir"):
            if self.files is None:
                raise ActionError("file transfer is disabled")
            return self._files(op, params, body)
        raise ActionError(f"unknown op {op!r}")

    async def _system(self, op: str, params: dict[str, Any]) -> dict[str, Any]:
        if op == "system_process_list":
            return await system.process_list()
        if op == "system_process_terminate":
            if not self.allow_process_terminate:
                raise ActionError("process termination is disabled")
            return await system.process_terminate(params.get("pid"))
        if op == "system_service_list":
            return await system.service_list(params.get("name"))
        if op == "system_service_control":
            name = params.get("name")
            if not isinstance(name, str) or name.casefold() not in self.service_allowlist:
                raise ActionError("service is not in the allowlist")
            return await system.service_control(params.get("action"), params.get("name"))
        if op == "system_task_list":
            return await system.task_list(params.get("name"))
        if op == "system_task_run":
            name = params.get("name")
            if not isinstance(name, str) or name.casefold() not in self.task_allowlist:
                raise ActionError("task is not in the allowlist")
            return await system.task_run(params.get("name"))
        if op == "system_diagnostics":
            return await system.diagnostics()
        raise ActionError(f"unknown system operation {op!r}")

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
