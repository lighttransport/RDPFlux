from __future__ import annotations

import asyncio
import ctypes
import os
from ctypes import wintypes

from .transport import AsyncTransport

WTS_CURRENT_SESSION = 0xFFFFFFFF
WTS_CHANNEL_OPTION_DYNAMIC = 0x00000001
ERROR_SEM_TIMEOUT = 121
ERROR_TIMEOUT = 1460


class WTSError(OSError):
    pass


class WTSChannelTransport(AsyncTransport):
    def __init__(self, handle: int, channel_name: str) -> None:
        if os.name != "nt":
            raise RuntimeError("WTS virtual channels are only available on Windows")
        self.handle = handle
        self.channel_name = channel_name
        self._closed = False
        self._write_lock = asyncio.Lock()
        self._wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        self._configure_api()

    def _configure_api(self) -> None:
        self._wts.WTSVirtualChannelRead.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        self._wts.WTSVirtualChannelRead.restype = wintypes.BOOL
        self._wts.WTSVirtualChannelWrite.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        self._wts.WTSVirtualChannelWrite.restype = wintypes.BOOL
        self._wts.WTSVirtualChannelClose.argtypes = [wintypes.HANDLE]
        self._wts.WTSVirtualChannelClose.restype = wintypes.BOOL

    @classmethod
    def open(cls, channel_name: str, *, dynamic: bool) -> "WTSChannelTransport":
        if os.name != "nt":
            raise RuntimeError("the RDP agent requires Windows")
        wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        wts.WTSVirtualChannelOpenEx.argtypes = [wintypes.DWORD, ctypes.c_char_p, wintypes.DWORD]
        wts.WTSVirtualChannelOpenEx.restype = wintypes.HANDLE
        flags = WTS_CHANNEL_OPTION_DYNAMIC if dynamic else 0
        handle = wts.WTSVirtualChannelOpenEx(WTS_CURRENT_SESSION, channel_name.encode("ascii"), flags)
        if not handle:
            error = ctypes.get_last_error()
            raise WTSError(error, f"cannot open RDP channel {channel_name}")
        return cls(handle, channel_name)

    def _blocking_read(self) -> bytes:
        while not self._closed:
            buffer = ctypes.create_string_buffer(64 * 1024)
            received = wintypes.ULONG()
            ok = self._wts.WTSVirtualChannelRead(self.handle, 1000, buffer, len(buffer), ctypes.byref(received))
            if ok:
                if received.value:
                    return buffer.raw[:received.value]
                continue
            error = ctypes.get_last_error()
            if error in (0, ERROR_SEM_TIMEOUT, ERROR_TIMEOUT):
                continue
            raise WTSError(error, f"read from RDP channel {self.channel_name} failed")
        return b""

    async def read(self) -> bytes:
        return await asyncio.to_thread(self._blocking_read)

    def _blocking_write(self, data: bytes) -> None:
        buffer = ctypes.create_string_buffer(data)
        written = wintypes.ULONG()
        if not self._wts.WTSVirtualChannelWrite(self.handle, buffer, len(data), ctypes.byref(written)):
            error = ctypes.get_last_error()
            raise WTSError(error, f"write to RDP channel {self.channel_name} failed")
        if written.value != len(data):
            raise WTSError(f"short RDP channel write: {written.value}/{len(data)}")

    async def write(self, data: bytes) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._blocking_write, data)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._wts.WTSVirtualChannelClose, self.handle)


def open_agent_transport(mode: str) -> WTSChannelTransport:
    errors: list[Exception] = []
    candidates = []
    if mode in ("auto", "dvc"):
        candidates.append(("com.rdpflux.v1", True))
    if mode in ("auto", "svc"):
        candidates.append(("rdp2tcp", False))
    for name, dynamic in candidates:
        try:
            return WTSChannelTransport.open(name, dynamic=dynamic)
        except Exception as exc:
            errors.append(exc)
    detail = "; ".join(str(error) for error in errors)
    raise WTSError(f"no compatible RDP virtual channel is available: {detail}")
