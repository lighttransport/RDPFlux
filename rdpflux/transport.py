from __future__ import annotations

import asyncio
import io
import struct
import sys
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable


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
        try:
            header = self._read_exact(self.stdin, 4)
            (length,) = struct.unpack("<I", header)
            if length > 1024 * 1024:
                raise ValueError(f"invalid FreeRDP message length {length}")
            return self._read_exact(self.stdin, length)
        except EOFError:
            return b""

    async def read(self) -> bytes:
        return await asyncio.to_thread(self._blocking_read)

    async def write(self, data: bytes) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._blocking_write, data)

    def _blocking_write(self, data: bytes) -> None:
        self.stdout.write(data)
        self.stdout.flush()

    async def close(self) -> None:
        self._closed = True


class CallbackTransport(AsyncTransport):
    """Thread-safe adapter for callback-driven DVC APIs."""

    def __init__(self, write_callback: Callable[[bytes], None]) -> None:
        self._write_callback = write_callback
        self._loop: asyncio.AbstractEventLoop | None = None
        self._incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._closed = False
        self._write_lock = asyncio.Lock()

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _submit_from_thread(self, value: bytes | None) -> None:
        loop = self._loop
        if loop is None or self._closed or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._incoming.put_nowait, value)
        except RuntimeError:
            # The loop was closed between the check above and the call.
            pass

    def feed_from_thread(self, data: bytes) -> None:
        self._submit_from_thread(bytes(data))

    def eof_from_thread(self) -> None:
        self._submit_from_thread(None)

    async def read(self) -> bytes:
        value = await self._incoming.get()
        return b"" if value is None else value

    async def write(self, data: bytes) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._write_callback, data)

    async def close(self) -> None:
        self._closed = True
        await self._incoming.put(None)

