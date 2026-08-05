from __future__ import annotations

import asyncio
import os
import signal
import uuid
from collections.abc import Awaitable, Callable

from .actions import ActionError

Output = Callable[[str], Awaitable[None]]


class PersistentShell:
    """A line-oriented PowerShell/Bash process with command sentinels."""

    def __init__(self, program: str = "powershell", cwd: str | None = None) -> None:
        self.program = program
        self.cwd = cwd
        self.process: asyncio.subprocess.Process | None = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        name = os.path.basename(self.program).lower()
        if name in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"):
            argv = [self.program, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "-"]
        elif name in ("bash", "bash.exe", "sh", "sh.exe"):
            argv = [self.program, "--noprofile", "--norc", "-s"]
        else:
            raise ActionError("shell must be powershell, pwsh, bash, or sh")
        try:
            self.process = await asyncio.create_subprocess_exec(
                *argv, cwd=self.cwd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
        except (OSError, ValueError) as exc:
            raise ActionError(f"cannot start shell: {exc}") from exc
        self._started = True

    async def run(self, command: str, output: Output) -> int | None:
        if not command.strip():
            raise ActionError("command must not be empty")
        await self.start()
        if self.process is None or self.process.stdin is None or self.process.stdout is None:
            raise ActionError("shell is not available")
        marker = f"__RDPFLUX_DONE_{uuid.uuid4().hex}__"
        name = os.path.basename(self.program).lower()
        if name in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"):
            script = f"{command}\n$__rdpflux_code = if ($?) {{ 0 }} else {{ 1 }}; " \
                     f"Write-Output ('{marker}:' + $__rdpflux_code)\n"
        else:
            script = f"{command}\nprintf '\\n{marker}:%s\\n' \"$?\"\n"
        self.process.stdin.write(script.encode("utf-8"))
        await self.process.stdin.drain()
        while True:
            line = await self.process.stdout.readline()
            if not line:
                return self.process.returncode
            text = line.decode("utf-8", "replace")
            if text.startswith(marker + ":"):
                try:
                    return int(text[len(marker) + 1:].strip())
                except ValueError:
                    return 1
            await output(text)

    async def interrupt(self) -> None:
        if self.process is None or self.process.returncode is not None:
            return
        if os.name == "nt":
            self.process.kill()
        else:
            self.process.send_signal(signal.SIGINT)

    async def close(self) -> None:
        if self.process is None or self.process.returncode is not None:
            return
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 1.0)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()
