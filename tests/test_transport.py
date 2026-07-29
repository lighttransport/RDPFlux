import asyncio
import io
import struct

import pytest

from rdpflux.transport import MAX_CALLBACK_BUFFER, CallbackTransport, FreeRDPStdioTransport


@pytest.mark.asyncio
async def test_freerdp_stdio_length_prefix_is_removed():
    source = io.BytesIO(struct.pack("<I", 5) + b"hello" + struct.pack("<I", 3) + b"bye")
    destination = io.BytesIO()
    transport = FreeRDPStdioTransport(source, destination)
    assert await transport.read() == b"hello"
    assert await transport.read() == b"bye"
    assert await transport.read() == b""
    await transport.write(b"raw-output")
    assert destination.getvalue() == b"raw-output"


@pytest.mark.asyncio
async def test_freerdp_rejects_zero_and_oversized_records():
    for length in (0, 1024 * 1024 + 1):
        transport = FreeRDPStdioTransport(io.BytesIO(struct.pack("<I", length)), io.BytesIO())
        with pytest.raises(ValueError, match="invalid FreeRDP message length"):
            await transport.read()


@pytest.mark.asyncio
async def test_freerdp_rejects_truncated_records():
    for data, message in ((b"\x01\x00", "header"), (struct.pack("<I", 5) + b"hi", "body")):
        transport = FreeRDPStdioTransport(io.BytesIO(data), io.BytesIO())
        with pytest.raises(ValueError, match=message):
            await transport.read()


@pytest.mark.asyncio
async def test_callback_transport_is_bounded_and_close_wakes_reader():
    transport = CallbackTransport(lambda _data: None)
    chunk = b"x" * (1024 * 1024)
    for _ in range(MAX_CALLBACK_BUFFER // len(chunk)):
        assert transport.feed_from_thread(chunk)
    with pytest.raises(BufferError, match="buffer limit"):
        transport.feed_from_thread(b"overflow")
    assert await transport.read() == chunk
    await transport.close()
    assert await transport.read() == b""
    assert not transport.feed_from_thread(b"late")
