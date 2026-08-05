import asyncio
import contextlib
import socket

import pytest

from rdpflux.config import AgentConfig, ClientConfig, Endpoint, ForwardRule
from rdpflux.forwarding import AgentForwarder, ClientForwarder, bridge_socket
from rdpflux.mux import CHUNK_SIZE, INITIAL_WINDOW, MAX_BUFFERED_DATA, MuxPeer, MuxStream
from rdpflux.transport import AsyncTransport, MemoryTransport


class LossyTransport(AsyncTransport):
    """Full-duplex in-memory transport that can drop selected writes.

    mstsc silently drops a dynamic virtual channel's first write, so this models
    a peer whose opening frames vanish on the wire.
    """

    def __init__(self, drop_writes: tuple[int, ...] = ()) -> None:
        self._incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.peer: "LossyTransport | None" = None
        self.closed = False
        self._writes = 0
        self._drop = set(drop_writes)

    @classmethod
    def pair(cls, drop_left: tuple[int, ...] = (), drop_right: tuple[int, ...] = ()):
        left, right = cls(drop_left), cls(drop_right)
        left.peer, right.peer = right, left
        return left, right

    async def read(self) -> bytes:
        value = await self._incoming.get()
        return b"" if value is None else value

    async def write(self, data: bytes) -> None:
        if self.closed or self.peer is None or self.peer.closed:
            raise ConnectionError("transport is closed")
        self._writes += 1
        if self._writes in self._drop:
            return  # the RDP stack accepted the write but dropped it on the wire
        await self.peer._incoming.put(bytes(data))

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self._incoming.put(None)
        if self.peer is not None:
            await self.peer._incoming.put(None)


async def start_echo_server():
    async def echo(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    return await asyncio.start_server(echo, "127.0.0.1", 0)


def unused_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.mark.asyncio
async def test_local_forward_large_payload():
    echo = await start_echo_server()
    echo_port = echo.sockets[0].getsockname()[1]
    left, right = MemoryTransport.pair()
    client_peer = MuxPeer(left, role="client")
    agent_peer = MuxPeer(right, role="agent")
    client = ClientForwarder(client_peer, ClientConfig(local_forwards=[
        ForwardRule(Endpoint("127.0.0.1", 0), Endpoint("127.0.0.1", echo_port))
    ]))
    agent = AgentForwarder(agent_peer, AgentConfig())
    await asyncio.gather(agent.start(), client.start())
    local_port = client.servers[0].sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
    payload = bytes(range(256)) * 4096
    writer.write(payload)
    await writer.drain()
    received = await reader.readexactly(len(payload))
    assert received == payload
    writer.close()
    await writer.wait_closed()
    await client.close()
    await agent.close()
    echo.close()
    await echo.wait_closed()


@pytest.mark.asyncio
async def test_direct_proxy_forwards_to_private_network_endpoint():
    echo = await start_echo_server()
    echo_port = echo.sockets[0].getsockname()[1]
    left, right = MemoryTransport.pair()
    client_peer = MuxPeer(left, role="client")
    agent_peer = MuxPeer(right, role="agent")
    client = ClientForwarder(client_peer, ClientConfig(proxy_forwards=[
        ForwardRule(Endpoint("127.0.0.1", 0), Endpoint("127.0.0.1", echo_port))
    ]))
    agent = AgentForwarder(agent_peer, AgentConfig())
    await asyncio.gather(agent.start(), client.start())
    local_port = client.servers[0].sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
    writer.write(b"direct private-network proxy")
    await writer.drain()
    assert await reader.readexactly(28) == b"direct private-network proxy"
    writer.close()
    await writer.wait_closed()
    await client.close()
    await agent.close()
    echo.close()
    await echo.wait_closed()


@pytest.mark.asyncio
async def test_handshake_survives_dropped_first_write():
    """mstsc drops the client's first channel write, losing its opening HELLO.

    The HELLO_ACK (a later write) still lands, so without retransmission both
    peers stay half-handshaken and time out. Retransmitting HELLO must recover.
    """
    left, right = LossyTransport.pair(drop_left=(1,))
    client = MuxPeer(left, role="client", keepalive_interval=0, handshake_retransmit=0.05)
    agent = MuxPeer(right, role="agent", keepalive_interval=0, handshake_retransmit=0.05)

    async def accept(_stream, _metadata):
        return None

    client.set_handlers(on_open=accept)
    agent.set_handlers(on_open=accept)
    await asyncio.gather(client.start(), agent.start())
    await asyncio.wait_for(asyncio.gather(client.wait_ready(), agent.wait_ready()), 2)
    assert client._ready.is_set() and agent._ready.is_set()

    stream = await client.open_stream({"kind": "tcp", "host": "127.0.0.1", "port": 1})
    assert stream.stream_id == 1
    await asyncio.gather(client.close(), agent.close())


@pytest.mark.asyncio
async def test_retransmitted_hello_is_re_acked():
    from rdpflux.protocol import Frame, FrameDecoder, MessageType, encode_control

    left, right = MemoryTransport.pair()
    peer = MuxPeer(left, role="agent", keepalive_interval=0, handshake_retransmit=0)
    hello = encode_control({"role": "client", "version": 1, "nonce": "a", "window": INITIAL_WINDOW})
    await peer._dispatch(Frame(MessageType.HELLO, payload=hello))
    # A retransmitted HELLO must be re-ACKed, not rejected as a duplicate: the peer
    # only resends because it never saw the first ACK.
    await peer._dispatch(Frame(MessageType.HELLO, payload=hello))
    decoder = FrameDecoder()
    acks = decoder.feed(await right.read()) + decoder.feed(await right.read())
    assert [frame.kind for frame in acks] == [MessageType.HELLO_ACK, MessageType.HELLO_ACK]
    await peer.close()


@pytest.mark.asyncio
async def test_changed_retransmitted_hello_is_rejected():
    from rdpflux.protocol import Frame, MessageType, encode_control, ProtocolError

    left, _right = MemoryTransport.pair()
    peer = MuxPeer(left, role="agent", keepalive_interval=0, handshake_retransmit=0)
    hello = {"role": "client", "version": 1, "nonce": "a", "window": INITIAL_WINDOW}
    await peer._dispatch(Frame(MessageType.HELLO, payload=encode_control(hello)))
    hello["nonce"] = "b"
    with pytest.raises(ProtocolError, match="changed"):
        await peer._dispatch(Frame(MessageType.HELLO, payload=encode_control(hello)))
    await peer.close()


def test_handshake_retransmit_rejects_invalid_values():
    left, _right = MemoryTransport.pair()
    for value in (-1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            MuxPeer(left, role="client", handshake_retransmit=value)


@pytest.mark.asyncio
async def test_late_open_result_does_not_close_mux():
    left, right = MemoryTransport.pair()
    client = MuxPeer(left, role="client", keepalive_interval=0)
    agent = MuxPeer(right, role="agent", keepalive_interval=0)

    async def delayed_open(_stream, _metadata):
        await asyncio.sleep(0.05)

    agent.set_handlers(on_open=delayed_open)
    await asyncio.gather(client.start(), agent.start())
    await asyncio.gather(client.wait_ready(), agent.wait_ready())
    with pytest.raises(asyncio.TimeoutError):
        await client.open_stream({"kind": "tcp"}, timeout=0.01)
    await asyncio.sleep(0.1)
    assert not client._closed.is_set()
    assert not client.streams
    await asyncio.gather(client.close(), agent.close())


@pytest.mark.asyncio
async def test_timed_out_listener_is_cancelled_without_closing_mux():
    left, right = MemoryTransport.pair()
    client = MuxPeer(left, role="client", keepalive_interval=0)
    agent = MuxPeer(right, role="agent", keepalive_interval=0)
    cancelled = asyncio.Event()

    async def delayed_listen(_request_id, _metadata):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
        return {}

    async def accept(_stream, _metadata):
        return None

    client.set_handlers(on_open=accept)
    agent.set_handlers(on_open=accept, on_listen=delayed_listen)
    await asyncio.gather(client.start(), agent.start())
    with pytest.raises(asyncio.TimeoutError):
        await client.request_listener({"kind": "reverse"}, timeout=0.01)
    await asyncio.wait_for(cancelled.wait(), 1)
    assert not client.closed
    assert not agent.closed
    await asyncio.gather(client.close(), agent.close())


@pytest.mark.asyncio
async def test_close_tolerates_full_receive_queue():
    left, _right = MemoryTransport.pair()
    peer = MuxPeer(left, role="client")
    stream = MuxStream(peer, 1)
    peer.streams[1] = stream
    for _ in range(INITIAL_WINDOW // CHUNK_SIZE):
        await stream._receive(b"x" * CHUNK_SIZE)
    await stream.close()
    assert peer._incoming_buffered == 0


@pytest.mark.asyncio
async def test_bridge_error_unblocks_other_direction():
    class FailingStream:
        def __init__(self):
            self.closed = asyncio.Event()
        async def write(self, _data):
            raise ConnectionError("write failed")
        async def read(self, _size):
            await self.closed.wait()
            return b""
        async def write_eof(self):
            return None
        async def close(self, _reason=""):
            self.closed.set()

    class Writer:
        def write(self, _data):
            return None
        async def drain(self):
            return None
        def can_write_eof(self):
            return False
        def close(self):
            return None
        async def wait_closed(self):
            return None

    reader = asyncio.StreamReader()
    reader.feed_data(b"trigger")
    await asyncio.wait_for(bridge_socket(reader, Writer(), FailingStream()), 1)


@pytest.mark.asyncio
async def test_socks5_connect():
    echo = await start_echo_server()
    echo_port = echo.sockets[0].getsockname()[1]
    left, right = MemoryTransport.pair()
    client = ClientForwarder(MuxPeer(left, role="client"), ClientConfig(socks=[Endpoint("127.0.0.1", 0)]))
    agent = AgentForwarder(MuxPeer(right, role="agent"), AgentConfig())
    await asyncio.gather(agent.start(), client.start())
    port = client.servers[0].sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    assert await reader.readexactly(2) == b"\x05\x00"
    writer.write(b"\x05\x01\x00\x01\x7f\x00\x00\x01" + echo_port.to_bytes(2, "big"))
    await writer.drain()
    assert (await reader.readexactly(10))[1] == 0
    writer.write(b"through socks")
    await writer.drain()
    assert await reader.readexactly(13) == b"through socks"
    writer.close()
    await writer.wait_closed()
    await client.close()
    await agent.close()
    echo.close()
    await echo.wait_closed()


@pytest.mark.asyncio
async def test_reverse_forward():
    echo = await start_echo_server()
    echo_port = echo.sockets[0].getsockname()[1]
    reverse_port = unused_port()
    left, right = MemoryTransport.pair()
    client = ClientForwarder(MuxPeer(left, role="client"), ClientConfig(reverse_forwards=[
        ForwardRule(Endpoint("127.0.0.1", reverse_port), Endpoint("127.0.0.1", echo_port), "test")
    ]))
    agent_cfg = AgentConfig(enable_reverse=True)
    agent = AgentForwarder(MuxPeer(right, role="agent"), agent_cfg)
    await asyncio.gather(agent.start(), client.start())
    reader, writer = await asyncio.open_connection("127.0.0.1", reverse_port)
    writer.write(b"reverse")
    await writer.drain()
    assert await reader.readexactly(7) == b"reverse"
    writer.close()
    await writer.wait_closed()
    await client.close()
    await agent.close()
    echo.close()
    await echo.wait_closed()


@pytest.mark.asyncio
async def test_simultaneous_close_does_not_kill_the_mux():
    """Both peers close independently, so CLOSE crosses the wire in each direction.

    Whichever arrives second finds the stream already removed. That used to raise
    ProtocolError("unknown stream N") and tear down the entire mux, killing every
    other connection over the tunnel.
    """
    left, right = MemoryTransport.pair()
    client_peer = MuxPeer(left, role="client")
    agent_peer = MuxPeer(right, role="agent")
    opened: list[MuxStream] = []

    async def accept(stream, _metadata):
        opened.append(stream)

    agent_peer.set_handlers(on_open=accept)
    client_peer.set_handlers(on_open=accept)
    await asyncio.gather(agent_peer.start(), client_peer.start())
    await asyncio.gather(client_peer.wait_ready(), agent_peer.wait_ready())

    stream = await client_peer.open_stream({"kind": "tcp", "host": "127.0.0.1", "port": 1})
    await asyncio.sleep(0.05)
    peer_stream = opened[0]

    # Close from both ends before either has processed the other's CLOSE.
    await asyncio.gather(stream.close("client done"), peer_stream.close("agent done"))
    await asyncio.sleep(0.1)

    assert not client_peer._closed.is_set(), "client mux must survive a crossing CLOSE"
    assert not agent_peer._closed.is_set(), "agent mux must survive a crossing CLOSE"

    # The tunnel must still carry new streams afterwards.
    second = await client_peer.open_stream({"kind": "tcp", "host": "127.0.0.1", "port": 2})
    assert second.stream_id != stream.stream_id

    await client_peer.close()
    await agent_peer.close()


@pytest.mark.asyncio
async def test_stale_frames_for_closed_streams_are_dropped():
    from rdpflux.protocol import Frame, MessageType, encode_control

    left, right = MemoryTransport.pair()
    peer = MuxPeer(left, role="client")
    peer._ready.set()
    peer._next_stream_id = 101  # local odd ID 99 was allocated and is now stale
    for frame in (
        Frame(MessageType.DATA, 99, b"late"),
        Frame(MessageType.WINDOW_UPDATE, 99, encode_control({"credit": 10})),
        Frame(MessageType.HALF_CLOSE, 99),
        Frame(MessageType.CLOSE, 99, encode_control({"reason": "gone"})),
    ):
        await peer._dispatch(frame)  # must not raise


@pytest.mark.asyncio
async def test_forged_frame_for_never_allocated_stream_is_rejected():
    from rdpflux.protocol import Frame, MessageType, ProtocolError

    left, _right = MemoryTransport.pair()
    peer = MuxPeer(left, role="client")
    peer._ready.set()
    with pytest.raises(ProtocolError, match="unknown stream"):
        await peer._dispatch(Frame(MessageType.DATA, 99, b"forged"))


@pytest.mark.asyncio
async def test_slow_open_handler_does_not_block_mux_dispatch():
    from rdpflux.protocol import Frame, MessageType, encode_control

    left, right = MemoryTransport.pair()
    peer = MuxPeer(left, role="agent", keepalive_interval=0)
    peer._ready.set()
    release = asyncio.Event()

    async def slow_open(_stream, _metadata):
        await release.wait()

    peer.set_handlers(on_open=slow_open)
    await peer._dispatch(Frame(MessageType.OPEN, 1, encode_control({"kind": "tcp"})))
    await asyncio.wait_for(peer._dispatch(Frame(MessageType.PING, payload=b"ping")), 0.1)
    assert await right.read()
    release.set()
    await peer.close()


@pytest.mark.asyncio
async def test_receive_window_is_enforced():
    from rdpflux.protocol import ProtocolError

    left, _right = MemoryTransport.pair()
    peer = MuxPeer(left, role="client")
    stream = MuxStream(peer, 1)
    peer.streams[1] = stream
    await stream._receive(b"x" * INITIAL_WINDOW)
    with pytest.raises(ProtocolError, match="receive window"):
        await stream._receive(b"x")
    await stream.close()


@pytest.mark.asyncio
async def test_close_while_waiting_for_aggregate_credit_does_not_underflow():
    left, _right = MemoryTransport.pair()
    peer = MuxPeer(left, role="client", keepalive_interval=0)
    peer._outgoing_buffered = MAX_BUFFERED_DATA
    stream = MuxStream(peer, 1)
    peer.streams[1] = stream

    writer = asyncio.create_task(stream.write(b"x" * CHUNK_SIZE))
    await asyncio.sleep(0)  # writer deducts stream credit, then blocks on aggregate credit
    await stream.close("test close")
    result = await asyncio.gather(writer, return_exceptions=True)
    await asyncio.sleep(0)  # allow aggregate-credit notifications to run

    assert isinstance(result[0], ConnectionError)
    assert peer._outgoing_buffered == MAX_BUFFERED_DATA
    assert stream._outgoing_reserved == 0


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_aggregate_credit_restores_stream_credit():
    left, _right = MemoryTransport.pair()
    peer = MuxPeer(left, role="client", keepalive_interval=0)
    peer._outgoing_buffered = MAX_BUFFERED_DATA
    stream = MuxStream(peer, 1)
    peer.streams[1] = stream

    writer = asyncio.create_task(stream.write(b"x" * CHUNK_SIZE))
    await asyncio.sleep(0)
    writer.cancel()
    result = await asyncio.gather(writer, return_exceptions=True)

    assert isinstance(result[0], asyncio.CancelledError)
    assert stream._send_credit == INITIAL_WINDOW
    assert stream._outgoing_reserved == 0
    assert peer._outgoing_buffered == MAX_BUFFERED_DATA


@pytest.mark.asyncio
async def test_write_eof_waits_for_an_active_write():
    from rdpflux.protocol import FrameDecoder, MessageType
    from rdpflux.transport import AsyncTransport

    class GatedTransport(AsyncTransport):
        def __init__(self):
            self.frames = []
            self.first_data = asyncio.Event()
            self.release = asyncio.Event()

        async def read(self):
            return b""

        async def write(self, data):
            self.frames.extend(FrameDecoder().feed(data))
            data_frames = [frame for frame in self.frames if frame.kind == MessageType.DATA]
            if self.frames[-1].kind == MessageType.DATA and len(data_frames) == 1:
                self.first_data.set()
                await self.release.wait()

        async def close(self):
            pass

    transport = GatedTransport()
    peer = MuxPeer(transport, role="client", keepalive_interval=0)
    stream = MuxStream(peer, 1)
    peer.streams[1] = stream

    writer = asyncio.create_task(stream.write(b"x" * (CHUNK_SIZE * 2)))
    await transport.first_data.wait()
    eof = asyncio.create_task(stream.write_eof())
    await asyncio.sleep(0)
    transport.release.set()
    await asyncio.gather(writer, eof)

    assert [frame.kind for frame in transport.frames] == [
        MessageType.DATA, MessageType.DATA, MessageType.HALF_CLOSE,
    ]


@pytest.mark.asyncio
async def test_graceful_close_preserves_buffered_data():
    left, _right = MemoryTransport.pair()
    peer = MuxPeer(left, role="client")
    stream = MuxStream(peer, 1)
    peer.streams[1] = stream
    await stream._receive(b"response")
    await stream._receive_eof()
    await stream._remote_close("done")
    assert not stream.remote_reset
    assert await stream.read() == b"response"
    assert await stream.read() == b""
    assert peer._incoming_buffered == 0
