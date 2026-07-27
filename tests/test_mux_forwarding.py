import asyncio
import contextlib
import socket

import pytest

from rdpflux.config import AgentConfig, ClientConfig, Endpoint, ForwardRule
from rdpflux.forwarding import AgentForwarder, ClientForwarder, bridge_socket
from rdpflux.mux import MuxPeer, MuxStream
from rdpflux.transport import MemoryTransport


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
async def test_close_tolerates_full_receive_queue():
    left, _right = MemoryTransport.pair()
    peer = MuxPeer(left, role="client")
    stream = MuxStream(peer, 1)
    peer.streams[1] = stream
    for _ in range(stream._incoming.maxsize):
        stream._incoming.put_nowait(b"x")
    await stream.close()


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
