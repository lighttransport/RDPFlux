import asyncio
import io
import struct

import pytest

from rdpflux.transport import FreeRDPStdioTransport


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
