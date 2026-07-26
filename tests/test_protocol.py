import pytest

from rdp2tcp.protocol import Frame, FrameDecoder, MessageType, ProtocolError


def test_incremental_frame_decode():
    encoded = Frame(MessageType.DATA, 7, b"hello").encode()
    decoder = FrameDecoder()
    assert decoder.feed(encoded[:3]) == []
    assert decoder.feed(encoded[3:-1]) == []
    assert decoder.feed(encoded[-1:]) == [Frame(MessageType.DATA, 7, b"hello")]
    decoder.finish()


def test_multiple_frames_and_bad_magic():
    decoder = FrameDecoder()
    data = Frame(MessageType.PING, payload=b"a").encode() + Frame(MessageType.PONG, payload=b"b").encode()
    assert [item.kind for item in decoder.feed(data)] == [MessageType.PING, MessageType.PONG]
    with pytest.raises(ProtocolError):
        FrameDecoder().feed(b"BAD!" + data[4:])

