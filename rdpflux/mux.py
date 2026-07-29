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
MAX_BUFFERED_DATA = 32 * 1024 * 1024

OpenHandler = Callable[["MuxStream", dict[str, Any]], Awaitable[None]]
ListenHandler = Callable[[int, dict[str, Any]], Awaitable[dict[str, Any]]]
CancelListenHandler = Callable[[int], Awaitable[None]]


class StreamClosed(ConnectionError):
    pass


def describe_exception(exc: BaseException) -> str:
    """Bare TimeoutError and friends stringify to '', which logs as a blank reason."""
    text = str(exc)
    return text if text else type(exc).__name__


class MuxStream:
    def __init__(self, peer: "MuxPeer", stream_id: int) -> None:
        self.peer = peer
        self.stream_id = stream_id
        self._read_buffer = bytearray()
        self._readable = asyncio.Event()
        self._send_credit = INITIAL_WINDOW
        self._outgoing_reserved = 0
        self._receive_credit = INITIAL_WINDOW
        self._credit_changed = asyncio.Condition()
        self._write_serial = asyncio.Lock()
        self._local_eof = False
        self._remote_eof = False
        self._closed = False
        self._remote_reset = False
        self._close_reason = ""

    @property
    def remote_reset(self) -> bool:
        """Whether the peer aborted the stream rather than half-closing it."""
        return self._remote_reset

    @property
    def close_reason(self) -> str:
        return self._close_reason

    async def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        while not self._read_buffer and not self._remote_eof and not self._closed:
            self._readable.clear()
            if self._read_buffer or self._remote_eof or self._closed:
                break
            await self._readable.wait()
        if not self._read_buffer:
            if self._closed:
                self.peer._remove_stream(self.stream_id)
            return b""
        count = len(self._read_buffer) if size < 0 else min(size, len(self._read_buffer))
        data = bytes(self._read_buffer[:count])
        del self._read_buffer[:count]
        self._receive_credit += count
        self.peer._release_incoming(count)
        if self._closed and not self._read_buffer:
            self.peer._remove_stream(self.stream_id)
        if not self._closed:
            await self.peer._send(Frame(
                MessageType.WINDOW_UPDATE, self.stream_id, encode_control({"credit": count}),
            ))
        return data

    async def write(self, data: bytes) -> None:
        async with self._write_serial:
            await self._write_serialized(data)

    async def _write_serialized(self, data: bytes) -> None:
        if self._local_eof or self._closed:
            raise StreamClosed("stream is not writable")
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            async with self._credit_changed:
                await self._credit_changed.wait_for(
                    lambda: self._send_credit > 0 or self._closed or self.peer.closed,
                )
                if self._closed or self.peer.closed:
                    raise StreamClosed("stream closed while waiting for credit")
                count = min(CHUNK_SIZE, self._send_credit, len(view) - offset)
                self._send_credit -= count
            try:
                await self.peer._reserve_outgoing(count, self)
            except BaseException:
                if not self._closed:
                    async with self._credit_changed:
                        self._send_credit += count
                        self._credit_changed.notify_all()
                raise
            self._outgoing_reserved += count
            if self._closed or self.peer.closed:
                self._release_outgoing_reserved(count)
                raise StreamClosed("stream closed before data could be sent")
            try:
                await self.peer._send(Frame(
                    MessageType.DATA, self.stream_id, bytes(view[offset:offset + count]),
                ))
            except BaseException:
                self._release_outgoing_reserved(count)
                if not self._closed:
                    async with self._credit_changed:
                        self._send_credit += count
                        self._credit_changed.notify_all()
                raise
            offset += count

    async def write_eof(self) -> None:
        async with self._write_serial:
            if not self._local_eof and not self._closed:
                self._local_eof = True
                await self.peer._send(Frame(MessageType.HALF_CLOSE, self.stream_id))

    async def close(self, reason: str = "") -> None:
        if self._closed:
            buffered = len(self._read_buffer)
            if buffered:
                self._read_buffer.clear()
                self._receive_credit = min(INITIAL_WINDOW, self._receive_credit + buffered)
                self.peer._release_incoming(buffered)
            self.peer._remove_stream(self.stream_id)
            return
        self._mark_closed(remote=False, reason=reason)
        with contextlib.suppress(Exception):
            await self.peer._send(Frame(
                MessageType.CLOSE, self.stream_id, encode_control({"reason": reason[:512]}),
            ))

    async def _receive(self, data: bytes) -> None:
        if not data:
            raise ProtocolError("empty DATA frame")
        if self._remote_eof or self._closed:
            raise ProtocolError("data received after stream closure")
        if len(data) > self._receive_credit:
            raise ProtocolError("stream exceeded its receive window")
        self.peer._reserve_incoming(len(data))
        self._receive_credit -= len(data)
        self._read_buffer.extend(data)
        self._readable.set()

    async def _receive_eof(self) -> None:
        if not self._remote_eof and not self._closed:
            self._remote_eof = True
            self._readable.set()

    async def _add_credit(self, amount: int) -> None:
        outstanding = INITIAL_WINDOW - self._send_credit
        if amount <= 0 or amount > outstanding or amount > self._outgoing_reserved:
            raise ProtocolError("invalid window update")
        async with self._credit_changed:
            self._send_credit += amount
            self._credit_changed.notify_all()
        self._release_outgoing_reserved(amount)

    async def _remote_close(self, reason: str = "") -> None:
        if self._closed:
            return
        if self._remote_eof:
            # CLOSE after HALF_CLOSE finalizes a graceful shutdown. Preserve data
            # already delivered before HALF_CLOSE until the application reads it.
            self._closed = True
            self._close_reason = reason[:512]
            if self._send_credit != INITIAL_WINDOW:
                self._send_credit = INITIAL_WINDOW
            self._release_outgoing_reserved()
            if not self._read_buffer:
                self.peer._remove_stream(self.stream_id)
            self._readable.set()
            async with self._credit_changed:
                self._credit_changed.notify_all()
            return
        self._remote_reset = True
        self._mark_closed(remote=True, reason=reason)

    def _mark_closed(self, *, remote: bool, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        self._remote_eof = True
        self._close_reason = reason[:512]
        if remote:
            self._remote_reset = True
        buffered = len(self._read_buffer)
        if buffered:
            self._read_buffer.clear()
            self._receive_credit = min(INITIAL_WINDOW, self._receive_credit + buffered)
            self.peer._release_incoming(buffered)
        if self._send_credit != INITIAL_WINDOW:
            self._send_credit = INITIAL_WINDOW
        self._release_outgoing_reserved()
        self.peer._remove_stream(self.stream_id)
        self._readable.set()

        async def notify() -> None:
            async with self._credit_changed:
                self._credit_changed.notify_all()

        with contextlib.suppress(RuntimeError):
            asyncio.create_task(notify())

    def _release_outgoing_reserved(self, amount: int | None = None) -> int:
        release = self._outgoing_reserved if amount is None else min(amount, self._outgoing_reserved)
        if release:
            self._outgoing_reserved -= release
        # A zero-byte release still wakes writers whose stream closed while they
        # were waiting for aggregate credit.
        self.peer._release_outgoing(release)
        return release


class MuxPeer:
    def __init__(self, transport: AsyncTransport, *, role: str, max_streams: int = 128,
                 keepalive_interval: float = 15.0, keepalive_timeout: float = 45.0) -> None:
        if role not in {"client", "agent"}:
            raise ValueError("role must be client or agent")
        if max_streams < 1:
            raise ValueError("max_streams must be positive")
        self.transport = transport
        self.role = role
        self.max_streams = max_streams
        self.keepalive_interval = keepalive_interval
        self.keepalive_timeout = keepalive_timeout
        self.streams: dict[int, MuxStream] = {}
        self._next_stream_id = 1 if role == "client" else 2
        self._highest_peer_id = 0
        self._decoder = FrameDecoder()
        self._write_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._last_received = time.monotonic()
        self._pending_opens: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._pending_listens: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._inbound_requests: dict[int, asyncio.Task[None]] = {}
        self._active_listeners: set[int] = set()
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._open_handler: OpenHandler | None = None
        self._listen_handler: ListenHandler | None = None
        self._cancel_listen_handler: CancelListenHandler | None = None
        self._hello_received = False
        self._hello_ack_received = False
        self._closing = False
        self.close_error: BaseException | None = None
        self._incoming_buffered = 0
        self._outgoing_buffered = 0
        self._outgoing_changed = asyncio.Condition()

    @property
    def closed(self) -> bool:
        return self._closed.is_set() or self._closing

    def set_handlers(self, *, on_open: OpenHandler, on_listen: ListenHandler | None = None,
                     on_cancel_listen: CancelListenHandler | None = None) -> None:
        self._open_handler = on_open
        self._listen_handler = on_listen
        self._cancel_listen_handler = on_cancel_listen

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
        await self._wait_event_or_closed(self._ready, timeout, "RDP channel disconnected during handshake")

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def _wait_event_or_closed(self, event: asyncio.Event, timeout: float, message: str) -> None:
        if event.is_set():
            return
        if self.closed:
            raise ConnectionError(message)
        ready = asyncio.create_task(event.wait())
        closed = asyncio.create_task(self._closed.wait())
        try:
            done, _ = await asyncio.wait(
                (ready, closed), timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise asyncio.TimeoutError
            if closed in done and not event.is_set():
                raise ConnectionError(message)
        finally:
            ready.cancel()
            closed.cancel()
            await asyncio.gather(ready, closed, return_exceptions=True)

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
            stream._mark_closed(remote=False, reason="open timed out")
            if sent:
                await self._send_close_best_effort(stream_id, "open timed out")
            raise
        except BaseException:
            stream._mark_closed(remote=False, reason="open failed")
            raise
        finally:
            self._pending_opens.pop(stream_id, None)

    async def request_listener(self, metadata: dict[str, Any], timeout: float = 15.0) -> int:
        await self.wait_ready(timeout)
        request_id = self._allocate_stream_id()
        future = asyncio.get_running_loop().create_future()
        self._pending_listens[request_id] = future
        sent = False
        try:
            await self._send(Frame(MessageType.LISTEN, request_id, encode_control(metadata)))
            sent = True
            result = await asyncio.wait_for(future, timeout)
            if not result.get("ok"):
                raise ConnectionError(str(result.get("error", "listen rejected")))
            return request_id
        except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
            if sent:
                await self._send_close_best_effort(request_id, "listen timed out")
            raise
        finally:
            self._pending_listens.pop(request_id, None)

    def _allocate_stream_id(self) -> int:
        value = self._next_stream_id
        if value <= 0 or value > 0xFFFFFFFF:
            raise RuntimeError("stream ID space exhausted; reconnect the tunnel")
        self._next_stream_id += 2
        return value

    def _remove_stream(self, stream_id: int) -> None:
        self.streams.pop(stream_id, None)

    async def _send(self, frame: Frame) -> None:
        if self._closed.is_set():
            raise ConnectionError("mux is closed")
        async with self._write_lock:
            await self.transport.write(frame.encode())

    async def _send_close_best_effort(self, stream_id: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await self._send(Frame(
                MessageType.CLOSE, stream_id, encode_control({"reason": reason[:512]}),
            ))

    async def _run(self) -> None:
        try:
            while True:
                data = await self.transport.read()
                if not data:
                    break
                frames = self._decoder.feed(data)
                for frame in frames:
                    self._last_received = time.monotonic()
                    await self._dispatch(frame)
            self._decoder.finish()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.close_error = exc
            LOG.warning("%s mux stopped: %s", self.role, describe_exception(exc))
        finally:
            await self._shutdown()

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._closed.is_set():
                await asyncio.sleep(self.keepalive_interval)
                idle = time.monotonic() - self._last_received
                if idle > self.keepalive_timeout:
                    error = TimeoutError(
                        f"RDP channel keepalive timed out after {idle:.1f}s idle "
                        f"(limit {self.keepalive_timeout:.1f}s)"
                    )
                    self.close_error = self.close_error or error
                    LOG.warning("%s %s", self.role, error)
                    await self.transport.close()
                    return
                await self._send(Frame(
                    MessageType.PING, payload=encode_control({"time": time.monotonic()}),
                ))
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.close_error = self.close_error or exc
            with contextlib.suppress(Exception):
                await self.transport.close()

    async def _dispatch(self, frame: Frame) -> None:
        self._validate_frame_shape(frame)
        if frame.kind == MessageType.HELLO:
            if self._hello_received:
                raise ProtocolError("duplicate HELLO")
            hello = decode_control(frame.payload)
            if hello.get("role") not in {"client", "agent"} or hello.get("role") == self.role:
                raise ProtocolError("invalid peer role")
            if hello.get("version") != 1:
                raise ProtocolError("unsupported peer protocol version")
            window = hello.get("window")
            if isinstance(window, bool) or not isinstance(window, int) or window != INITIAL_WINDOW:
                raise ProtocolError("unsupported peer stream window")
            self._hello_received = True
            await self._send(Frame(
                MessageType.HELLO_ACK, payload=encode_control({"ok": True, "version": 1}),
            ))
            self._maybe_ready()
            return
        if frame.kind == MessageType.HELLO_ACK:
            if self._hello_ack_received:
                raise ProtocolError("duplicate HELLO_ACK")
            ack = decode_control(frame.payload)
            if ack.get("ok") is not True or ack.get("version") != 1:
                raise ProtocolError("handshake rejected")
            self._hello_ack_received = True
            self._maybe_ready()
            return
        if not self._ready.is_set():
            raise ProtocolError("message received before handshake")
        if frame.kind == MessageType.OPEN:
            await self._start_open(frame)
        elif frame.kind == MessageType.OPEN_RESULT:
            result = self._decode_result(frame.payload, "OPEN_RESULT")
            future = self._pending_opens.get(frame.stream_id)
            if future is None or future.done():
                if not self._is_stale_id(frame.stream_id):
                    raise ProtocolError("unexpected OPEN_RESULT")
                LOG.debug("dropping late OPEN_RESULT for request %d", frame.stream_id)
                return
            future.set_result(result)
        elif frame.kind == MessageType.DATA:
            stream = self._live_stream(frame)
            if stream is not None:
                await stream._receive(frame.payload)
        elif frame.kind == MessageType.WINDOW_UPDATE:
            stream = self._live_stream(frame)
            if stream is not None:
                control = decode_control(frame.payload)
                credit = control.get("credit")
                if isinstance(credit, bool) or not isinstance(credit, int):
                    raise ProtocolError("window credit must be an integer")
                await stream._add_credit(credit)
        elif frame.kind == MessageType.HALF_CLOSE:
            stream = self._live_stream(frame)
            if stream is not None:
                await stream._receive_eof()
        elif frame.kind == MessageType.CLOSE:
            await self._handle_close(frame)
        elif frame.kind == MessageType.LISTEN:
            await self._start_listen(frame)
        elif frame.kind == MessageType.LISTEN_RESULT:
            result = self._decode_result(frame.payload, "LISTEN_RESULT")
            future = self._pending_listens.get(frame.stream_id)
            if future is None or future.done():
                if not self._is_stale_id(frame.stream_id):
                    raise ProtocolError("unexpected LISTEN_RESULT")
                LOG.debug("dropping late LISTEN_RESULT for request %d", frame.stream_id)
                return
            future.set_result(result)
        elif frame.kind == MessageType.PING:
            await self._send(Frame(MessageType.PONG, payload=frame.payload))
        elif frame.kind != MessageType.PONG:
            raise ProtocolError(f"unexpected message {frame.kind.name}")

    def _validate_frame_shape(self, frame: Frame) -> None:
        if frame.flags:
            raise ProtocolError("protocol-v1 frame flags must be zero")
        global_kinds = {
            MessageType.HELLO, MessageType.HELLO_ACK, MessageType.PING, MessageType.PONG,
        }
        if frame.kind in global_kinds and frame.stream_id != 0:
            raise ProtocolError(f"{frame.kind.name} must use stream ID zero")
        if frame.kind not in global_kinds and frame.stream_id == 0:
            raise ProtocolError(f"{frame.kind.name} requires a stream ID")
        if frame.kind == MessageType.DATA and not frame.payload:
            raise ProtocolError("empty DATA frame")
        if frame.kind == MessageType.HALF_CLOSE and frame.payload:
            raise ProtocolError("HALF_CLOSE must not have a payload")

    def _maybe_ready(self) -> None:
        if self._hello_received and self._hello_ack_received:
            self._ready.set()

    @staticmethod
    def _decode_result(payload: bytes, label: str) -> dict[str, Any]:
        result = decode_control(payload)
        if not isinstance(result.get("ok"), bool):
            raise ProtocolError(f"{label} ok field must be a boolean")
        error = result.get("error")
        if error is not None and not isinstance(error, str):
            raise ProtocolError(f"{label} error field must be a string")
        return result

    def _validate_peer_request_id(self, stream_id: int) -> None:
        expected_parity = 0 if self.role == "client" else 1
        if stream_id % 2 != expected_parity:
            raise ProtocolError("invalid peer stream ID parity")
        if stream_id <= self._highest_peer_id:
            raise ProtocolError("peer stream IDs must increase monotonically")
        self._highest_peer_id = stream_id

    def _is_stale_id(self, stream_id: int) -> bool:
        local_parity = 1 if self.role == "client" else 0
        if stream_id % 2 == local_parity:
            return 0 < stream_id < self._next_stream_id
        return 0 < stream_id <= self._highest_peer_id

    def _live_stream(self, frame: Frame) -> MuxStream | None:
        stream = self.streams.get(frame.stream_id)
        if stream is not None:
            return stream
        if not self._is_stale_id(frame.stream_id):
            raise ProtocolError(f"{frame.kind.name} references an unknown stream")
        LOG.debug("dropping %s for closed stream %d", frame.kind.name, frame.stream_id)
        return None

    async def _start_open(self, frame: Frame) -> None:
        self._validate_peer_request_id(frame.stream_id)
        if len(self.streams) >= self.max_streams or self._open_handler is None:
            await self._reject_open(frame.stream_id)
            return
        metadata = decode_control(frame.payload)
        stream = MuxStream(self, frame.stream_id)
        self.streams[frame.stream_id] = stream
        self._spawn_request(frame.stream_id, self._finish_open(stream, metadata))

    async def _reject_open(self, stream_id: int) -> None:
        await self._send(Frame(
            MessageType.OPEN_RESULT, stream_id,
            encode_control({"ok": False, "error": "stream rejected"}),
        ))

    async def _finish_open(self, stream: MuxStream, metadata: dict[str, Any]) -> None:
        try:
            assert self._open_handler is not None
            await self._open_handler(stream, metadata)
            await self._send(Frame(
                MessageType.OPEN_RESULT, stream.stream_id, encode_control({"ok": True}),
            ))
        except asyncio.CancelledError:
            await stream._remote_close("open cancelled")
            raise
        except Exception as exc:
            await stream._remote_close(describe_exception(exc))
            await self._send(Frame(
                MessageType.OPEN_RESULT, stream.stream_id,
                encode_control({"ok": False, "error": describe_exception(exc)[:512]}),
            ))

    async def _start_listen(self, frame: Frame) -> None:
        self._validate_peer_request_id(frame.stream_id)
        metadata = decode_control(frame.payload)
        if (self._listen_handler is None
                or len(self._active_listeners) + len(self._inbound_requests) >= self.max_streams):
            await self._send(Frame(
                MessageType.LISTEN_RESULT, frame.stream_id,
                encode_control({"ok": False, "error": "remote listener limit reached"}),
            ))
            return
        self._spawn_request(frame.stream_id, self._finish_listen(frame.stream_id, metadata))

    async def _finish_listen(self, request_id: int, metadata: dict[str, Any]) -> None:
        if self._listen_handler is None or len(self._active_listeners) >= self.max_streams:
            result = {"ok": False, "error": "remote listeners are unsupported or limited"}
        else:
            try:
                result = await self._listen_handler(request_id, metadata)
                if not isinstance(result, dict):
                    raise TypeError("listener handler must return an object")
                result = {**result, "ok": True}
                self._active_listeners.add(request_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result = {"ok": False, "error": describe_exception(exc)[:512]}
        await self._send(Frame(
            MessageType.LISTEN_RESULT, request_id, encode_control(result),
        ))

    async def _handle_close(self, frame: Frame) -> None:
        control = decode_control(frame.payload)
        reason = control.get("reason", "")
        if not isinstance(reason, str):
            raise ProtocolError("close reason must be a string")
        task = self._inbound_requests.get(frame.stream_id)
        if task is not None:
            task.cancel()
        stream = self.streams.get(frame.stream_id)
        if stream is not None:
            await stream._remote_close(reason)
            return
        if frame.stream_id in self._active_listeners:
            self._active_listeners.discard(frame.stream_id)
            if self._cancel_listen_handler is not None:
                self._spawn_handler(self._cancel_listen_handler(frame.stream_id))
            return
        if not self._is_stale_id(frame.stream_id):
            raise ProtocolError("CLOSE references an unknown request")

    def _spawn_request(self, request_id: int, coroutine: Awaitable[None]) -> None:
        task = self._spawn_handler(coroutine)
        self._inbound_requests[request_id] = task

        def remove(_task: asyncio.Task[None]) -> None:
            if self._inbound_requests.get(request_id) is _task:
                self._inbound_requests.pop(request_id, None)

        task.add_done_callback(remove)

    def _spawn_handler(self, coroutine: Awaitable[None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine)
        self._handler_tasks.add(task)

        def done(completed: asyncio.Task[None]) -> None:
            self._handler_tasks.discard(completed)
            if completed.cancelled():
                return
            exc = completed.exception()
            if exc is not None and not self.closed:
                self.close_error = self.close_error or exc
                LOG.warning("%s mux handler failed: %s", self.role, describe_exception(exc))
                asyncio.create_task(self.transport.close())

        task.add_done_callback(done)
        return task

    def _reserve_incoming(self, amount: int) -> None:
        if self._incoming_buffered + amount > MAX_BUFFERED_DATA:
            raise ProtocolError("mux exceeded its aggregate receive buffer")
        self._incoming_buffered += amount

    def _release_incoming(self, amount: int) -> None:
        self._incoming_buffered = max(0, self._incoming_buffered - amount)

    async def _reserve_outgoing(self, amount: int, stream: MuxStream) -> None:
        async with self._outgoing_changed:
            await self._outgoing_changed.wait_for(
                lambda: self._outgoing_buffered + amount <= MAX_BUFFERED_DATA
                or stream._closed or self.closed,
            )
            if stream._closed or self.closed:
                raise StreamClosed("mux closed while waiting for aggregate credit")
            self._outgoing_buffered += amount

    def _release_outgoing(self, amount: int) -> None:
        self._outgoing_buffered = max(0, self._outgoing_buffered - amount)

        async def notify() -> None:
            async with self._outgoing_changed:
                self._outgoing_changed.notify_all()

        with contextlib.suppress(RuntimeError):
            asyncio.create_task(notify())

    async def close(self) -> None:
        if self._closed.is_set():
            return
        if self._runner and self._runner is not asyncio.current_task():
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
        else:
            await self._shutdown()

    async def _shutdown(self) -> None:
        if self._closed.is_set():
            return
        if self._closing:
            await self._closed.wait()
            return
        self._closing = True
        if self._heartbeat and self._heartbeat is not asyncio.current_task():
            self._heartbeat.cancel()
            await asyncio.gather(self._heartbeat, return_exceptions=True)
        current = asyncio.current_task()
        tasks = [task for task in self._handler_tasks if task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for stream in list(self.streams.values()):
            await stream._remote_close("RDP channel disconnected")
        self.streams.clear()
        if self._cancel_listen_handler is not None and self._active_listeners:
            await asyncio.gather(
                *(self._cancel_listen_handler(request_id)
                  for request_id in list(self._active_listeners)),
                return_exceptions=True,
            )
        self._active_listeners.clear()
        for future in [*self._pending_opens.values(), *self._pending_listens.values()]:
            if not future.done():
                future.set_exception(ConnectionError("RDP channel disconnected"))
        async with self._outgoing_changed:
            self._outgoing_changed.notify_all()
        with contextlib.suppress(Exception):
            await self.transport.close()
        self._closed.set()
