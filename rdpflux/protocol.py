from __future__ import annotations

import enum
import json
import struct
from dataclasses import dataclass
from typing import Any

MAGIC = b"R2TP"
VERSION = 1
HEADER = struct.Struct("!4sBBHII")
MAX_PAYLOAD = 16 * 1024
MAX_CONTROL_PAYLOAD = 16 * 1024


class ProtocolError(Exception):
    pass


class MessageType(enum.IntEnum):
    HELLO = 1
    HELLO_ACK = 2
    OPEN = 3
    OPEN_RESULT = 4
    DATA = 5
    WINDOW_UPDATE = 6
    HALF_CLOSE = 7
    CLOSE = 8
    LISTEN = 9
    LISTEN_RESULT = 10
    PING = 11
    PONG = 12


@dataclass(slots=True, frozen=True)
class Frame:
    kind: MessageType
    stream_id: int = 0
    payload: bytes = b""
    flags: int = 0

    def encode(self) -> bytes:
        if len(self.payload) > MAX_PAYLOAD:
            raise ProtocolError(f"payload exceeds {MAX_PAYLOAD} bytes")
        return HEADER.pack(MAGIC, VERSION, int(self.kind), self.flags, self.stream_id, len(self.payload)) + self.payload


class FrameDecoder:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self._buffer.extend(data)
        frames: list[Frame] = []
        while len(self._buffer) >= HEADER.size:
            magic, version, raw_kind, flags, stream_id, length = HEADER.unpack_from(self._buffer)
            if magic != MAGIC:
                preview = bytes(self._buffer[:HEADER.size])
                raise ProtocolError(
                    f"invalid frame magic {magic!r}; first {len(preview)} bytes {preview.hex(' ')}"
                )
            if version != VERSION:
                raise ProtocolError(f"unsupported protocol version {version}")
            if length > MAX_PAYLOAD:
                raise ProtocolError(f"payload length {length} exceeds limit")
            total = HEADER.size + length
            if len(self._buffer) < total:
                break
            try:
                kind = MessageType(raw_kind)
            except ValueError as exc:
                raise ProtocolError(f"unknown message type {raw_kind}") from exc
            payload = bytes(self._buffer[HEADER.size:total])
            del self._buffer[:total]
            frames.append(Frame(kind, stream_id, payload, flags))
        return frames

    def finish(self) -> None:
        if self._buffer:
            raise ProtocolError("transport closed with a partial frame")


def encode_control(value: dict[str, Any]) -> bytes:
    data = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_CONTROL_PAYLOAD:
        raise ProtocolError("control message is too large")
    return data


def decode_control(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid control message") from exc
    if not isinstance(value, dict):
        raise ProtocolError("control payload must be an object")
    return value

