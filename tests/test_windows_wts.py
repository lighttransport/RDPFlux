import ctypes
import os

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="WTS virtual channels are Windows-only")

from rdpflux.windows_wts import (  # noqa: E402
    BENIGN_READ_ERRORS,
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


def test_idle_read_timeout_is_not_fatal():
    """An idle poll must not tear the channel down.

    A dynamic channel reports an expired read timeout as ERROR_IO_INCOMPLETE (996)
    rather than ERROR_SEM_TIMEOUT, which previously killed the tunnel one second
    after the handshake.
    """
    assert 996 in BENIGN_READ_ERRORS  # ERROR_IO_INCOMPLETE
    assert 997 in BENIGN_READ_ERRORS  # ERROR_IO_PENDING
    assert 121 in BENIGN_READ_ERRORS  # ERROR_SEM_TIMEOUT
    assert 1460 in BENIGN_READ_ERRORS  # ERROR_TIMEOUT
    assert 0 in BENIGN_READ_ERRORS
    # Genuine channel failures must still propagate.
    assert 6 not in BENIGN_READ_ERRORS  # ERROR_INVALID_HANDLE
    assert 109 not in BENIGN_READ_ERRORS  # ERROR_BROKEN_PIPE


def _fake_transport():
    """A WTSChannelTransport with the ctypes plumbing replaced by a recorder."""
    import ctypes
    import threading

    from rdpflux.windows_wts import WTSChannelTransport

    transport = object.__new__(WTSChannelTransport)
    transport.handle = 1
    transport.channel_name = "test"
    transport._closed = False
    transport._write_buffer = ctypes.create_string_buffer(CHANNEL_CHUNK_LENGTH)
    transport._io_lock = threading.Lock()
    writes = []

    class FakeWts:
        @staticmethod
        def WTSVirtualChannelWrite(_handle, buffer, size, written):
            writes.append(bytes(buffer[:size]))
            written._obj.value = size
            return 1

    transport._wts = FakeWts()
    return transport, writes


def test_write_splits_at_the_chunk_limit():
    transport, writes = _fake_transport()
    payload = bytes(range(256)) * 20  # 5120 bytes
    transport._blocking_write(payload)
    assert len(writes) == 4, "5120 bytes must split into four chunks"
    assert all(len(chunk) <= CHANNEL_CHUNK_LENGTH for chunk in writes)
    assert b"".join(writes) == payload, "chunking must preserve the byte stream"


def test_write_reuses_one_buffer_and_ignores_empty():
    transport, writes = _fake_transport()
    before = ctypes.addressof(transport._write_buffer)
    transport._blocking_write(b"first")
    transport._blocking_write(b"second-and-longer")
    assert ctypes.addressof(transport._write_buffer) == before, "buffer must not be reallocated"
    assert writes == [b"first", b"second-and-longer"], "stale bytes must not leak between writes"
    transport._blocking_write(b"")
    assert len(writes) == 2, "empty writes must not reach the channel"


def test_oversized_message_is_rejected():
    reassembler = ChunkReassembler(max_message=16)
    with pytest.raises(WTSError, match="exceeds"):
        reassembler.feed(chunk(b"x", 1024, CHANNEL_FLAG_FIRST | CHANNEL_FLAG_LAST))


def test_pending_tracks_incomplete_reassembly():
    """A message that never completes stalls the read loop and starves the keepalive."""
    reassembler = ChunkReassembler()
    assert reassembler.pending == 0
    assert reassembler.feed(chunk(b"half", 8, CHANNEL_FLAG_FIRST)) is None
    assert reassembler.pending == 4, "partial message must be visible"
    assert reassembler.feed(chunk(b"done", 8, CHANNEL_FLAG_LAST)) == b"halfdone"
    assert reassembler.pending == 0, "state must clear once the message completes"


def test_continuation_requires_first_and_consistent_length():
    reassembler = ChunkReassembler()
    with pytest.raises(WTSError, match="CHANNEL_FLAG_FIRST"):
        reassembler.feed(chunk(b"late", 4, CHANNEL_FLAG_LAST))
    assert reassembler.feed(chunk(b"half", 8, CHANNEL_FLAG_FIRST)) is None
    with pytest.raises(WTSError, match="changed"):
        reassembler.feed(chunk(b"done", 9, CHANNEL_FLAG_LAST))
    assert reassembler.pending == 0


def test_zero_length_message_is_rejected():
    with pytest.raises(WTSError, match="zero-length"):
        ChunkReassembler().feed(chunk(b"", 0, CHANNEL_FLAG_FIRST | CHANNEL_FLAG_LAST))
