from __future__ import annotations

import binascii
import struct
import zlib

# Kept free of ctypes.wintypes so the encoders stay importable — and testable —
# on any platform, while capture.py holds the Windows-only GDI calls.


def target_size(native_width: int, native_height: int, width: int) -> tuple[int, int]:
    """Fit `width` to the display's aspect ratio, never upscaling.

    Sending more pixels than the display has costs tunnel bandwidth and image
    tokens without adding any detail.
    """
    if native_width <= 0 or native_height <= 0:
        raise ValueError("display has no area")
    width = max(1, min(width, native_width))
    return width, max(1, round(width * native_height / native_width))


def bgra_to_rgb(bgra: bytes) -> bytearray:
    """Drop the alpha channel and swap to RGB order.

    Extended-slice assignment on a bytearray moves each channel in one C-level
    pass; a per-pixel Python loop would take seconds at 1080p.
    """
    rgb = bytearray(len(bgra) // 4 * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]
    return rgb


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", binascii.crc32(tag + payload) & 0xFFFFFFFF))


def encode_png(rgb: bytes, width: int, height: int, level: int = 6) -> bytes:
    """Minimal PNG encoder over stdlib zlib, so the agent needs no dependencies."""
    expected = width * height * 3
    if len(rgb) != expected:
        raise ValueError(f"expected {expected} bytes for {width}x{height}, got {len(rgb)}")
    stride = width * 3
    raw = bytearray()
    for row in range(height):
        raw.append(0)  # filter type 0 (None)
        raw += rgb[row * stride:(row + 1) * stride]
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", zlib.compress(bytes(raw), level))
            + _png_chunk(b"IEND", b""))


def encode_jpeg(rgb: bytes, width: int, height: int, quality: int) -> bytes:
    """JPEG via Pillow when the optional [control] extra is installed."""
    from io import BytesIO

    from PIL import Image

    image = Image.frombuffer("RGB", (width, height), bytes(rgb), "raw", "RGB", 0, 1)
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def have_jpeg() -> bool:
    try:
        import PIL.Image  # noqa: F401
    except ImportError:
        return False
    return True
