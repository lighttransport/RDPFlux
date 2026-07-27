import os

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="WTS virtual channels are Windows-only")

from rdpflux.windows_wts import (  # noqa: E402
    CHANNEL_CHUNK_LENGTH,
    CHANNEL_FLAG_FIRST,
    CHANNEL_FLAG_LAST,
    CHANNEL_PACKET_COMPRESSED,
    CHANNEL_PDU_HEADER,
    ChunkReassembler,
    WTSError,
)


def chunk(payload: bytes, total: int, flags: int) -> bytes:
    return CHANNEL_PDU_HEADER.pack(total, flags) + payload


def test_single_chunk_message():
    reassembler = ChunkReassembler()
    body = b"R2TP-payload"
    assert reassembler.feed(chunk(body, len(body), CHANNEL_FLAG_FIRST | CHANNEL_FLAG_LAST)) == body


def test_multi_chunk_message_is_reassembled():
    reassembler = ChunkReassembler()
    body = bytes(range(256)) * 20  # 5120 bytes -> four chunks
    parts = [body[i:i + CHANNEL_CHUNK_LENGTH] for i in range(0, len(body), CHANNEL_CHUNK_LENGTH)]
    assert len(parts) == 4
    for index, part in enumerate(parts):
        flags = 0
        if index == 0:
            flags |= CHANNEL_FLAG_FIRST
        if index == len(parts) - 1:
            flags |= CHANNEL_FLAG_LAST
        result = reassembler.feed(chunk(part, len(body), flags))
        if index < len(parts) - 1:
            assert result is None, "message must not complete before CHANNEL_FLAG_LAST"
        else:
            assert result == body


def test_first_flag_discards_a_truncated_message():
    reassembler = ChunkReassembler()
    assert reassembler.feed(chunk(b"stale", 99, CHANNEL_FLAG_FIRST)) is None
    body = b"fresh"
    assert reassembler.feed(chunk(body, len(body), CHANNEL_FLAG_FIRST | CHANNEL_FLAG_LAST)) == body


def test_length_mismatch_is_rejected():
    reassembler = ChunkReassembler()
    with pytest.raises(WTSError, match="header declared"):
        reassembler.feed(chunk(b"four", 99, CHANNEL_FLAG_FIRST | CHANNEL_FLAG_LAST))
    # State is cleared, so the channel can carry on with the next message.
    assert reassembler.feed(chunk(b"ok", 2, CHANNEL_FLAG_FIRST | CHANNEL_FLAG_LAST)) == b"ok"


def test_compressed_and_truncated_chunks_are_rejected():
    reassembler = ChunkReassembler()
    with pytest.raises(WTSError, match="compressed"):
        reassembler.feed(chunk(b"x", 1, CHANNEL_FLAG_FIRST | CHANNEL_FLAG_LAST | CHANNEL_PACKET_COMPRESSED))
    with pytest.raises(WTSError, match="CHANNEL_PDU_HEADER"):
        reassembler.feed(b"\x00\x01\x02")


def test_captured_dvc_hello_ack_chunk():
    """A real HELLO_ACK read off a WTS_CHANNEL_OPTION_DYNAMIC channel.

    Dynamic channels carry CHANNEL_PDU_HEADER too, so the header must be stripped
    before the frame decoder ever sees the payload.
    """
    from rdpflux.protocol import FrameDecoder, MessageType

    captured = bytes.fromhex(
        "27000000"          # length = 39 (the reassembled message)
        "03000000"          # CHANNEL_FLAG_FIRST | CHANNEL_FLAG_LAST
        "52325450"          # R2TP
        "01"                # version 1
        "02"                # HELLO_ACK
        "0000"              # flags
        "00000000"          # stream id
        "00000017"          # payload length = 23
    ) + b'{"ok":true,"version":1}'

    message = ChunkReassembler().feed(captured)
    assert message is not None
    assert len(message) == 39
    assert message.startswith(b"R2TP")

    frames = FrameDecoder().feed(message)
    assert [frame.kind for frame in frames] == [MessageType.HELLO_ACK]


def test_oversized_message_is_rejected():
    reassembler = ChunkReassembler(max_message=16)
    with pytest.raises(WTSError, match="exceeds"):
        reassembler.feed(chunk(b"x", 1024, CHANNEL_FLAG_FIRST | CHANNEL_FLAG_LAST))
