from __future__ import annotations

import asyncio
import logging
from typing import Any

from .actions import ActionError

LOG = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 300.0
MAX_OUTPUT = 1024 * 1024
MAX_ARGS = 256


def _decode(data: bytes) -> tuple[str, bool]:
    truncated = len(data) > MAX_OUTPUT
    return data[:MAX_OUTPUT].decode("utf-8", "replace"), truncated


async def run(params: dict[str, Any]) -> dict[str, Any]:
    """Run a command and capture its output.

    The command is an argv list and is never passed through a shell, so quoting
    and metacharacters carry no meaning. To use a shell, ask for one explicitly:
    ["powershell", "-NoProfile", "-Command", "..."].
    """
    command = params.get("command")
    if not isinstance(command, list) or not command:
        raise ActionError("command must be a non-empty array of arguments")
    if len(command) > MAX_ARGS:
        raise ActionError(f"command exceeds {MAX_ARGS} arguments")
    if not all(isinstance(item, str) for item in command):
        raise ActionError("every command argument must be a string")

    timeout = params.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ActionError("timeout must be a number")
    if not 0 < timeout <= MAX_TIMEOUT:
        raise ActionError(f"timeout must be between 0 and {MAX_TIMEOUT} seconds")

    cwd = params.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ActionError("cwd must be a string")

    try:
        process = await asyncio.create_subprocess_exec(
            *command, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        raise ActionError(f"cannot run {command[0]}: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (asyncio.TimeoutError, TimeoutError):
        process.kill()
        await process.wait()
        raise ActionError(f"command timed out after {timeout} seconds")

    out, out_truncated = _decode(stdout)
    err, err_truncated = _decode(stderr)
    return {
        "exit_code": process.returncode,
        "stdout": out,
        "stderr": err,
        "truncated": out_truncated or err_truncated,
    }
