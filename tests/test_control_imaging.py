import struct
import zlib

import pytest

from rdpflux.control.imaging import bgra_to_rgb, encode_png, target_size


def test_bgra_to_rgb_swaps_channels_and_drops_alpha():
    # Two pixels: opaque red, then opaque blue, in BGRA order.
    bgra = bytes([0, 0, 255, 255, 255, 0, 0, 255])
    assert bytes(bgra_to_rgb(bgra)) == bytes([255, 0, 0, 0, 0, 255])


def test_bgra_to_rgb_handles_a_full_row():
    pixels = 1000
    bgra = bytes([1, 2, 3, 255]) * pixels
    assert bytes(bgra_to_rgb(bgra)) == bytes([3, 2, 1]) * pixels


@pytest.mark.parametrize("native, width, expected", [
    ((2560, 1440), 1280, (1280, 720)),
    ((2560, 1440), 1920, (1920, 1080)),
    ((1920, 1080), 1920, (1920, 1080)),
    ((1366, 768), 1920, (1366, 768)),  # never upscale
    ((2560, 1440), 2576, (2560, 1440)),
])
def test_target_size_preserves_aspect_and_never_upscales(native, width, expected):
    assert target_size(*native, width) == expected


def test_target_size_rejects_an_empty_display():
    with pytest.raises(ValueError, match="no area"):
        target_size(0, 0, 1280)


def _png_chunks(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset, chunks = 8, {}
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset:offset + 4])
        tag = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        assert struct.unpack(">I", data[offset + 8 + length:offset + 12 + length])[0] == \
            zlib.crc32(tag + payload) & 0xFFFFFFFF, f"bad CRC on {tag!r}"
        chunks[tag] = payload
        offset += 12 + length
    return chunks


def test_encode_png_structure_and_pixels():
    width, height = 3, 2
    rgb = bytes([
        255, 0, 0, 0, 255, 0, 0, 0, 255,
        10, 20, 30, 40, 50, 60, 70, 80, 90,
    ])
    chunks = _png_chunks(encode_png(rgb, width, height))
    assert set(chunks) == {b"IHDR", b"IDAT", b"IEND"}
    assert struct.unpack(">IIBBBBB", chunks[b"IHDR"]) == (width, height, 8, 2, 0, 0, 0)

    # Every scanline is prefixed with filter byte 0, so the pixels survive intact.
    raw = zlib.decompress(chunks[b"IDAT"])
    stride = width * 3
    assert len(raw) == height * (stride + 1)
    for row in range(height):
        start = row * (stride + 1)
        assert raw[start] == 0
        assert raw[start + 1:start + 1 + stride] == rgb[row * stride:(row + 1) * stride]


def test_encode_png_rejects_a_mismatched_buffer():
    with pytest.raises(ValueError, match="expected 27 bytes"):
        encode_png(bytes(10), 3, 3)
