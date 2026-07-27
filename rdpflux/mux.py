from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .protocol import Frame, FrameDecoder, MessageType, ProtocolError, decode_control, encode_control
from .transport import AsyncTransport

LOG = logging.getLogger(__name__)
INITIAL_WINDOW = 256 * 1024
CHUNK_SIZE = 16 * 1024
_EOF = object()

OpenHandler = Callable[["MuxStream", dict[str, Any]], Awaitable[None]]
ListenHandler = Callable[[int, dict[str, Any]], Awaitable[dict[str, Any]]]


class StreamClosed(ConnectionError):
    pass


class MuxStream:
    def __init__(self, peer: "MuxPeer", stream_id: int) -> None:
        self.peer = peer
        self.stream_id = stream_id
        self._incoming: asyncio.Queue[bytes | object] = asyncio.Queue(maxsize=32)
        self._read_buffer = bytearray()
        self._send_credit = INITIAL_WINDOW
        self._credit_changed = asyncio.Condition()
        self._local_eof = False
        self._remote_eof = False
        self._closed = False

    async def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        while not self._read_buffer and not self._remote_eof:
            item = await self._incoming.get()
            if item is _EOF:
                self._remote_eof = True
                break
            self._read_buffer.extend(item)  # type: ignore[arg-type]
        if not self._read_buffer:
            return b""
        count = len(self._read_buffer) if size < 0 else min(size, len(self._read_buffer))
        data = bytes(self._read_buffer[:count])
        del self._read_buffer[:count]
        await self.peer._send(Frame(MessageType.WINDOW_UPDATE, self.stream_id, encode_control({"credit": count})))
        return data

    async def write(self, data: bytes) -> None:
        if self._local_eof or self._closed:
            raise StreamClosed("stream is not writable")
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            async with self._credit_changed:
                await self._credit_changed.wait_for(lambda: self._send_credit > 0 or self._closed)
                if self._closed:
                    raise StreamClosed("stream closed while waiting for credit")
                count = min(CHUNK_SIZE, self._send_credit, len(view) - offset)
                self._send_credit -= count
            await self.peer._send(Frame(MessageType.DATA, self.stream_id, bytes(view[offset:offset + count])))
            offset += count

    async def write_eof(self) -> None:
        if not self._local_eof and not self._closed:
            self._local_eof = True
            await self.peer._send(Frame(MessageType.HALF_CLOSE, self.stream_id))

    async def close(self, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        await self.peer._send(Frame(MessageType.CLOSE, self.stream_id, encode_control({"reason": reason[:512]})))
        self.peer._remove_stream(self.stream_id)
        async with self._credit_changed:
            self._credit_changed.notify_all()
        with contextlib.suppress(asyncio.QueueFull):
            self._incoming.put_nowait(_EOF)

    async def _receive(self, data: bytes) -> None:
        if self._remote_eof or self._closed:
            raise ProtocolError("data received after stream closure")
        await self._incoming.put(data)

    async def _receive_eof(self) -> None:
        if not self._remote_eof:
            self._remote_eof = True
            await self._incoming.put(_EOF)

    async def _add_credit(self, amount: int) -> None:
        if amount <= 0 or amount > INITIAL_WINDOW:
            raise ProtocolError("invalid window update")
        async with self._credit_changed:
            self._send_credit = min(INITIAL_WINDOW, self._send_credit + amount)
            self._credit_changed.notify_all()

    async def _remote_close(self) -> None:
        self._closed = True
        self._remote_eof = True
        with contextlib.suppress(asyncio.QueueFull):
            self._incoming.put_nowait(_EOF)
        async with self._credit_changed:
            self._credit_changed.notify_all()


class MuxPeer:
    def __init__(self, transport: AsyncTransport, *, role: str, max_streams: int = 128,
                 keepalive_interval: float = 15.0, keepalive_timeout: float = 45.0) -> None:
        if role not in {"client", "agent"}:
            raise ValueError("role must be client or agent")
        self.transport = transport
        self.role = role
        self.max_streams = max_streams
        self.keepalive_interval = keepalive_interval
        self.keepalive_timeout = keepalive_timeout
        self.streams: dict[int, MuxStream] = {}
        self._next_stream_id = 1 if role == "client" else 2
        self._decoder = FrameDecoder()
        self._write_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._last_received = time.monotonic()
        self._pending_opens: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._abandoned_opens: set[int] = set()
        self._pending_listens: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._open_handler: OpenHandler | None = None
        self._listen_handler: ListenHandler | None = None

    def set_handlers(self, *, on_open: OpenHandler, on_listen: ListenHandler | None = None) -> None:
        self._open_handler = on_open
        self._listen_handler = on_listen

    async def start(self) -> None:
        if self._runner is not None:
            return
        self._runner = asyncio.create_task(self._run(), name=f"mux-{self.role}")
        if self.keepalive_interval > 0:
            self._heartbeat = asyncio.create_task(self._heartbeat_loop(), name=f"heartbeat-{self.role}")
        await self._send(Frame(MessageType.HELLO, payload=encode_control({
            "role": self.role, "version": 1, "nonce": secrets.token_hex(16), "window": INITIAL_WINDOW,
        })))

    async def wait_ready(self, timeout: float = 15.0) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def open_stream(self, metadata: dict[str, Any], timeout: float = 15.0) -> MuxStream:
        await self.wait_ready(timeout)
        if len(self.streams) >= self.max_streams:
            raise ConnectionError("stream limit reached")
        stream_id = self._allocate_stream_id()
        stream = MuxStream(self, stream_id)
        self.streams[stream_id] = stream
        future = asyncio.get_running_loop().create_future()
        self._pending_opens[stream_id] = future
        sent = False
        try:
            await self._send(Frame(MessageType.OPEN, stream_id, encode_control(metadata)))
            sent = True
            result = await asyncio.wait_for(future, timeout)
            if not result.get("ok"):
                raise ConnectionError(str(result.get("error", "open rejected")))
            return stream
        except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
            if sent:
                self._abandoned_opens.add(stream_id)
            else:
                self._remove_stream(stream_id)
            raise
        except BaseException:
            self._remove_stream(stream_id)
            raise
        finally:
            self._pending_opens.pop(stream_id, None)

    async def request_listener(self, metadata: dict[str, Any], timeout: float = 15.0) -> int:
        await self.wait_ready(timeout)
        request_id = self._allocate_stream_id()
        future = asyncio.get_running_loop().create_future()
        self._pending_listens[request_id] = future
        try:
            await self._send(Frame(MessageType.LISTEN, request_id, encode_control(metadata)))
            result = await asyncio.wait_for(future, timeout)
            if not result.get("ok"):
                raise ConnectionError(str(result.get("error", "listen rejected")))
            return request_id
        finally:
            self._pending_listens.pop(request_id, None)

    def _allocate_stream_id(self) -> int:
        for _ in range(0x7FFFFFFF):
            value = self._next_stream_id
            self._next_stream_id = (self._next_stream_id + 2) & 0xFFFFFFFF
            if value and value not in self.streams and value not in self._pending_listens:
                return value
        raise RuntimeError("stream ID space exhausted")

    def _remove_stream(self, stream_id: int) -> None:
        self.streams.pop(stream_id, None)

    async def _send(self, frame: Frame) -> None:
        async with self._write_lock:
            await self.transport.write(frame.encode())

    async def _run(self) -> None:
        try:
            while True:
                data = await self.transport.read()
                if not data:
                    break
                self._last_received = time.monotonic()
                for frame in self._decoder.feed(data):
                    await self._dispatch(frame)
            self._decoder.finish()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("%s mux stopped: %s", self.role, exc)
        finally:
            await self._shutdown()

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._closed.is_set():
                await asyncio.sleep(self.keepalive_interval)
                if time.monotonic() - self._last_received > self.keepalive_timeout:
                    LOG.warning("%s RDP channel keepalive timed out", self.role)
                    await self.transport.close()
                    return
                await self._send(Frame(MessageType.PING, payload=encode_control({"time": time.monotonic()})))
        except (asyncio.CancelledError, ConnectionError, OSError):
            return

    async def _dispatch(self, frame: Frame) -> None:
        if frame.kind == MessageType.HELLO:
            hello = decode_control(frame.payload)
            if hello.get("role") == self.role:
                raise ProtocolError("peer has the same role")
            await self._send(Frame(MessageType.HELLO_ACK, payload=encode_control({"ok": True, "version": 1})))
            self._ready.set()
            return
        if frame.kind == MessageType.HELLO_ACK:
            if not decode_control(frame.payload).get("ok"):
                raise ProtocolError("handshake rejected")
            self._ready.set()
            return
        if not self._ready.is_set():
            raise ProtocolError("message received before handshake")
        if frame.kind == MessageType.OPEN:
            await self._handle_open(frame)
        elif frame.kind == MessageType.OPEN_RESULT:
            future = self._pending_opens.get(frame.stream_id)
            if frame.stream_id in self._abandoned_opens:
                result = decode_control(frame.payload)
                self._abandoned_opens.discard(frame.stream_id)
                if result.get("ok"):
                    await self._send(Frame(MessageType.CLOSE, frame.stream_id,
                                           encode_control({"reason": "open timed out"})))
                self._remove_stream(frame.stream_id)
                return
            if future is None or future.done():
                raise ProtocolError("unexpected OPEN_RESULT")
            future.set_result(decode_control(frame.payload))
        elif frame.kind == MessageType.DATA:
            if frame.stream_id in self._abandoned_opens:
                return
            await self._stream(frame.stream_id)._receive(frame.payload)
        elif frame.kind == MessageType.WINDOW_UPDATE:
            if frame.stream_id in self._abandoned_opens:
                return
            await self._stream(frame.stream_id)._add_credit(int(decode_control(frame.payload).get("credit", 0)))
        elif frame.kind == MessageType.HALF_CLOSE:
            if frame.stream_id in self._abandoned_opens:
                return
            await self._stream(frame.stream_id)._receive_eof()
        elif frame.kind == MessageType.CLOSE:
            if frame.stream_id in self._abandoned_opens:
                self._remove_stream(frame.stream_id)
                return
            stream = self._stream(frame.stream_id)
            await stream._remote_close()
            self._remove_stream(frame.stream_id)
        elif frame.kind == MessageType.LISTEN:
            await self._handle_listen(frame)
        elif frame.kind == MessageType.LISTEN_RESULT:
            future = self._pending_listens.get(frame.stream_id)
            if future is None or future.done():
                raise ProtocolError("unexpected LISTEN_RESULT")
            future.set_result(decode_control(frame.payload))
        elif frame.kind == MessageType.PING:
            await self._send(Frame(MessageType.PONG, payload=frame.payload))
        elif frame.kind != MessageType.PONG:
            raise ProtocolError(f"unexpected message {frame.kind.name}")

    def _stream(self, stream_id: int) -> MuxStream:
        try:
            return self.streams[stream_id]
        except KeyError as exc:
            raise ProtocolError(f"unknown stream {stream_id}") from exc

    async def _handle_open(self, frame: Frame) -> None:
        expected_parity = 0 if self.role == "client" else 1
        if frame.stream_id % 2 != expected_parity or frame.stream_id in self.streams:
            raise ProtocolError("invalid peer stream ID")
        if len(self.streams) >= self.max_streams or self._open_handler is None:
            await self._send(Frame(MessageType.OPEN_RESULT, frame.stream_id, encode_control({"ok": False, "error": "stream rejected"})))
            return
        stream = MuxStream(self, frame.stream_id)
        metadata = decode_control(frame.payload)
        self.streams[frame.stream_id] = stream
        try:
            await self._open_handler(stream, metadata)
            await self._send(Frame(MessageType.OPEN_RESULT, frame.stream_id, encode_control({"ok": True})))
        except Exception as exc:
            self._remove_stream(frame.stream_id)
            await self._send(Frame(MessageType.OPEN_RESULT, frame.stream_id, encode_control({"ok": False, "error": str(exc)[:512]})))

    async def _handle_listen(self, frame: Frame) -> None:
        if self._listen_handler is None:
            result = {"ok": False, "error": "remote listeners are unsupported"}
        else:
            try:
                result = await self._listen_handler(frame.stream_id, decode_control(frame.payload))
                result = {"ok": True, **result}
            except Exception as exc:
                result = {"ok": False, "error": str(exc)[:512]}
        await self._send(Frame(MessageType.LISTEN_RESULT, frame.stream_id, encode_control(result)))

    async def close(self) -> None:
        if self._runner:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
        else:
            await self._shutdown()

    async def _shutdown(self) -> None:
        if self._closed.is_set():
            return
        if self._heartbeat and self._heartbeat is not asyncio.current_task():
            self._heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat
        for stream in list(self.streams.values()):
            await stream._remote_close()
        self.streams.clear()
        self._abandoned_opens.clear()
        error = ConnectionError("RDP channel disconnected")
        for future in [*self._pending_opens.values(), *self._pending_listens.values()]:
            if not future.done():
                future.set_exception(error)
        with contextlib.suppress(Exception):
            await self.transport.close()
        self._closed.set()
