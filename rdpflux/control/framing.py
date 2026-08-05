from __future__ import annotations

import json
from typing import Any

from ..mux import MuxStream

# A control message is one JSON header line followed by an optional raw body.
# Binary stays out of the JSON so a screenshot does not pay base64's 33% overhead
# on a channel that writes in 1600-byte chunks.
MAX_HEADER = 64 * 1024
MAX_BODY = 128 * 1024 * 1024
READ_CHUNK = 64 * 1024


class FramingError(Exception):
    pass


def encode_message(header: dict[str, Any], body: bytes = b"") -> bytes:
    if body:
        header = {**header, "body_len": len(body)}
    line = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(line) > MAX_HEADER:
        raise FramingError(f"control header exceeds {MAX_HEADER} bytes")
    return line + b"\n" + body


class MessageReader:
    """Read length-delimited control messages from a mux stream."""

    def __init__(self, stream: MuxStream) -> None:
        self._stream = stream
        self._buffer = bytearray()
        self._eof = False

    async def _fill(self) -> bool:
        """Pull one more chunk. Returns False at end of stream."""
        if self._eof:
            return False
        data = await self._stream.read(READ_CHUNK)
        if not data:
            self._eof = True
            return False
        self._buffer.extend(data)
        return True

    async def _read_line(self) -> bytes | None:
        while True:
            index = self._buffer.find(b"\n")
            if index >= 0:
                line = bytes(self._buffer[:index])
                del self._buffer[:index + 1]
                return line
            if len(self._buffer) > MAX_HEADER:
                raise FramingError(f"control header exceeds {MAX_HEADER} bytes")
            if not await self._fill():
                if self._buffer:
                    raise FramingError("stream ended mid-header")
                return None

    async def _read_exactly(self, count: int) -> bytes:
        while len(self._buffer) < count:
            if not await self._fill():
                raise FramingError(
                    f"stream ended after {len(self._buffer)} of {count} body bytes"
                )
        body = bytes(self._buffer[:count])
        del self._buffer[:count]
        return body

    async def read_message(self) -> tuple[dict[str, Any], bytes] | None:
        """Return (header, body), or None once the peer has closed the stream."""
        line = await self._read_line()
        if line is None:
            return None
        try:
            header = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FramingError(f"invalid control header: {exc}") from exc
        if not isinstance(header, dict):
            raise FramingError("control header must be a JSON object")

        length = header.get("body_len", 0)
        if isinstance(length, bool) or not isinstance(length, int) or length < 0:
            raise FramingError("body_len must be a non-negative integer")
        if length > MAX_BODY:
            raise FramingError(f"control body of {length} exceeds {MAX_BODY} bytes")
        return header, await self._read_exactly(length) if length else b""
