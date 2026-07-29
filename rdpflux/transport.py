from __future__ import annotations

import asyncio
import io
import queue
import struct
import sys
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable

MAX_FREERDP_MESSAGE = 1024 * 1024
MAX_CALLBACK_MESSAGE = 1024 * 1024
MAX_CALLBACK_BUFFER = 4 * 1024 * 1024
MAX_CALLBACK_MESSAGES = 4096


class AsyncTransport(ABC):
    @abstractmethod
    async def read(self) -> bytes:
        """Return bytes, or b'' at EOF."""

    @abstractmethod
    async def write(self, data: bytes) -> None:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass


class StreamTransport(AsyncTransport):
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    async def read(self) -> bytes:
        return await self.reader.read(64 * 1024)

    async def write(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def close(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()


class MemoryTransport(AsyncTransport):
    """In-memory full-duplex transport used by integration tests."""

    def __init__(self) -> None:
        self._incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.peer: MemoryTransport | None = None
        self.closed = False

    @classmethod
    def pair(cls) -> tuple["MemoryTransport", "MemoryTransport"]:
        left, right = cls(), cls()
        left.peer, right.peer = right, left
        return left, right

    async def read(self) -> bytes:
        value = await self._incoming.get()
        return b"" if value is None else value

    async def write(self, data: bytes) -> None:
        if self.closed or self.peer is None or self.peer.closed:
            raise ConnectionError("transport is closed")
        await self.peer._incoming.put(bytes(data))

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self._incoming.put(None)
        if self.peer is not None:
            await self.peer._incoming.put(None)


class FreeRDPStdioTransport(AsyncTransport):
    """Bridge FreeRDP's rdp2tcp child-process framing to a byte stream.

    FreeRDP prefixes every server-to-child channel write with a native little-endian
    uint32 length. Child-to-server bytes are copied directly into channel writes.
    """

    def __init__(self, stdin: io.BufferedReader | None = None, stdout: io.BufferedWriter | None = None) -> None:
        self._owns_stdio = stdin is None and stdout is None
        self.stdin = stdin or sys.stdin.buffer
        self.stdout = stdout or sys.stdout.buffer
        self._closed = False
        self._write_lock = asyncio.Lock()

    @staticmethod
    def _read_exact(stream: io.BufferedReader, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            data = stream.read(length - len(chunks))
            if not data:
                raise EOFError
            chunks.extend(data)
        return bytes(chunks)

    def _blocking_read(self) -> bytes:
        if self._closed:
            return b""
        first = self.stdin.read(4)
        if not first:
            return b""
        try:
            header = first + self._read_exact(self.stdin, 4 - len(first)) if len(first) < 4 else first
        except EOFError as exc:
            raise ValueError("truncated FreeRDP message header") from exc
        (length,) = struct.unpack("<I", header)
        if length == 0 or length > MAX_FREERDP_MESSAGE:
            raise ValueError(f"invalid FreeRDP message length {length}")
        try:
            return self._read_exact(self.stdin, length)
        except EOFError as exc:
            raise ValueError(f"truncated FreeRDP message body (expected {length} bytes)") from exc

    async def read(self) -> bytes:
        return await asyncio.to_thread(self._blocking_read)

    async def write(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("transport is closed")
        if not data:
            return
        async with self._write_lock:
            if self._closed:
                raise ConnectionError("transport is closed")
            await asyncio.to_thread(self._blocking_write, data)

    def _blocking_write(self, data: bytes) -> None:
        self.stdout.write(data)
        self.stdout.flush()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_stdio:
            for stream in (self.stdin, self.stdout):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


class CallbackTransport(AsyncTransport):
    """Thread-safe adapter for callback-driven DVC APIs."""

    def __init__(self, write_callback: Callable[[bytes], None]) -> None:
        self._write_callback = write_callback
        self._incoming: queue.Queue[bytes | None] = queue.Queue(maxsize=MAX_CALLBACK_MESSAGES)
        self._state_lock = threading.Lock()
        self._queued_bytes = 0
        self._closed = False
        self._write_lock = asyncio.Lock()

    def feed_from_thread(self, data: bytes) -> bool:
        value = bytes(data)
        if not value:
            return True
        if len(value) > MAX_CALLBACK_MESSAGE:
            raise BufferError(f"DVC callback of {len(value)} bytes exceeds the message limit")
        with self._state_lock:
            if self._closed:
                return False
            if self._queued_bytes + len(value) > MAX_CALLBACK_BUFFER or self._incoming.full():
                raise BufferError("DVC callback buffer limit exceeded")
            self._incoming.put_nowait(value)
            self._queued_bytes += len(value)
        return True

    def eof_from_thread(self) -> None:
        self._close_from_thread()

    async def read(self) -> bytes:
        value = await asyncio.to_thread(self._incoming.get)
        with self._state_lock:
            if value is not None:
                self._queued_bytes = max(0, self._queued_bytes - len(value))
            closed = self._closed
        if closed:
            return b""
        return b"" if value is None else value

    async def write(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("transport is closed")
        if not data:
            return
        async with self._write_lock:
            if self._closed:
                raise ConnectionError("transport is closed")
            await asyncio.to_thread(self._write_callback, data)

    async def close(self) -> None:
        self._close_from_thread()

    def _close_from_thread(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            while True:
                try:
                    value = self._incoming.get_nowait()
                except queue.Empty:
                    break
                if value is not None:
                    self._queued_bytes = max(0, self._queued_bytes - len(value))
            self._incoming.put_nowait(None)
