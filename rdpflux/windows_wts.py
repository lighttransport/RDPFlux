from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import struct
from ctypes import wintypes

from .transport import AsyncTransport

LOG = logging.getLogger(__name__)
WTS_CURRENT_SESSION = 0xFFFFFFFF
WTS_CHANNEL_OPTION_DYNAMIC = 0x00000001
ERROR_SEM_TIMEOUT = 121
ERROR_IO_INCOMPLETE = 996
ERROR_IO_PENDING = 997
ERROR_TIMEOUT = 1460

# A read that simply ran out its timeout with no data is normal for a polling
# loop and must not tear the channel down. WTSVirtualChannelRead reports that as
# ERROR_IO_INCOMPLETE on a dynamic channel rather than one of the timeout codes;
# ERROR_IO_PENDING is the same "no data yet" condition from overlapped I/O.
BENIGN_READ_ERRORS = frozenset({0, ERROR_SEM_TIMEOUT, ERROR_IO_INCOMPLETE, ERROR_IO_PENDING, ERROR_TIMEOUT})

# MS-RDPBCGR 2.2.6.1: static virtual channel data is chunked, and every chunk
# carries a CHANNEL_PDU_HEADER whose length field is the size of the whole
# reassembled message rather than of the chunk.
CHANNEL_PDU_HEADER = struct.Struct("<II")
CHANNEL_CHUNK_LENGTH = 1600
CHANNEL_FLAG_FIRST = 0x00000001
CHANNEL_FLAG_LAST = 0x00000002
CHANNEL_PACKET_COMPRESSED = 0x00200000
MAX_SVC_MESSAGE = 1024 * 1024


class WTSError(OSError):
    pass


class ChunkReassembler:
    """Rebuild static-virtual-channel messages from CHANNEL_PDU_HEADER chunks.

    Each WTSVirtualChannelRead returns exactly one chunk, so chunk boundaries are
    read boundaries; only the payloads need to be accumulated across reads.
    """

    def __init__(self, max_message: int = MAX_SVC_MESSAGE) -> None:
        self._message = bytearray()
        self._max_message = max_message

    def feed(self, chunk: bytes) -> bytes | None:
        """Return the completed message, or None while more chunks are pending."""
        if len(chunk) < CHANNEL_PDU_HEADER.size:
            raise WTSError(f"channel chunk shorter than CHANNEL_PDU_HEADER: {len(chunk)} bytes")
        length, flags = CHANNEL_PDU_HEADER.unpack_from(chunk)
        if flags & CHANNEL_PACKET_COMPRESSED:
            raise WTSError("compressed virtual channel data is not supported")
        if length > self._max_message:
            raise WTSError(f"channel message length {length} exceeds {self._max_message}")
        if flags & CHANNEL_FLAG_FIRST:
            self._message.clear()
        self._message.extend(chunk[CHANNEL_PDU_HEADER.size:])
        if len(self._message) > self._max_message:
            self._message.clear()
            raise WTSError("channel message exceeded the reassembly limit")
        if not flags & CHANNEL_FLAG_LAST:
            return None
        if len(self._message) != length:
            actual = len(self._message)
            self._message.clear()
            raise WTSError(f"channel message reassembled to {actual} bytes, header declared {length}")
        message = bytes(self._message)
        self._message.clear()
        return message


class WTSChannelTransport(AsyncTransport):
    def __init__(self, handle: int, channel_name: str, *, dynamic: bool = True) -> None:
        if os.name != "nt":
            raise RuntimeError("WTS virtual channels are only available on Windows")
        self.handle = handle
        self.channel_name = channel_name
        self.dynamic = dynamic
        self._closed = False
        self._write_lock = asyncio.Lock()
        # WTSVirtualChannelRead prefixes every chunk with CHANNEL_PDU_HEADER even
        # when the channel was opened with WTS_CHANNEL_OPTION_DYNAMIC, so reads are
        # reassembled on both channel types. Writes stay unheadered either way: the
        # RDP stack adds the header itself.
        self._reassembler = ChunkReassembler()
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
        return cls(handle, channel_name, dynamic=dynamic)

    def _blocking_read(self) -> bytes:
        while not self._closed:
            buffer = ctypes.create_string_buffer(64 * 1024)
            received = wintypes.ULONG()
            ok = self._wts.WTSVirtualChannelRead(self.handle, 1000, buffer, len(buffer), ctypes.byref(received))
            if ok:
                if received.value:
                    data = buffer.raw[:received.value]
                    LOG.debug("channel read %d bytes: %s", len(data), data[:32].hex(" "))
                    message = self._reassembler.feed(data)
                    if message is None:
                        continue  # mid-message; wait for the remaining chunks
                    return message
                continue
            error = ctypes.get_last_error()
            if error in BENIGN_READ_ERRORS:
                continue
            raise WTSError(error, f"read from RDP channel {self.channel_name} failed")
        return b""

    async def read(self) -> bytes:
        return await asyncio.to_thread(self._blocking_read)

    def _write_all(self, data: bytes) -> None:
        buffer = ctypes.create_string_buffer(data, len(data))
        written = wintypes.ULONG()
        if not self._wts.WTSVirtualChannelWrite(self.handle, buffer, len(data), ctypes.byref(written)):
            error = ctypes.get_last_error()
            raise WTSError(error, f"write to RDP channel {self.channel_name} failed")
        if written.value != len(data):
            raise WTSError(f"short RDP channel write: {written.value}/{len(data)}")

    def _blocking_write(self, data: bytes) -> None:
        if not data:
            return
        # The RDP stack adds CHANNEL_PDU_HEADER itself; we only respect the chunk
        # limit. Our own frame decoder reassembles across the resulting messages.
        for offset in range(0, len(data), CHANNEL_CHUNK_LENGTH):
            self._write_all(data[offset:offset + CHANNEL_CHUNK_LENGTH])

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
