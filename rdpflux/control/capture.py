from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes

from .imaging import bgra_to_rgb, encode_jpeg, encode_png, have_jpeg, target_size

__all__ = [
    "CaptureError", "bgra_to_rgb", "encode_jpeg", "encode_png", "ensure_dpi_aware",
    "grab", "have_jpeg", "virtual_screen",
]

LOG = logging.getLogger(__name__)

SRCCOPY = 0x00CC0020
HALFTONE = 4
BI_RGB = 0
DIB_RGB_COLORS = 0
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4


class CaptureError(OSError):
    pass


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def ensure_dpi_aware() -> None:
    """Report true pixels from GetSystemMetrics on a scaled display.

    Without this a 150%-scaled 2560x1440 desktop reports 1706x960, and every
    captured frame is a blurry upscale of the wrong region.
    """
    user32 = ctypes.windll.user32
    try:
        if user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        ):
            return
    except AttributeError:
        pass  # pre-1703 Windows
    try:
        user32.SetProcessDPIAware()
    except Exception:
        LOG.warning("could not enable DPI awareness; captures may be scaled")


def virtual_screen() -> tuple[int, int, int, int]:
    """Origin and size of the whole virtual desktop, spanning every monitor."""
    metric = ctypes.windll.user32.GetSystemMetrics
    return (
        metric(SM_XVIRTUALSCREEN), metric(SM_YVIRTUALSCREEN),
        metric(SM_CXVIRTUALSCREEN), metric(SM_CYVIRTUALSCREEN),
    )


def grab(width: int) -> tuple[bytearray, int, int, int, int]:
    """Capture the virtual desktop, downscaled to `width`.

    Returns (rgb, delivered_width, delivered_height, native_width, native_height).
    StretchBlt does the resampling inside GDI, so no Python-level pixel loop runs.
    """
    if os.name != "nt":
        raise CaptureError("screen capture requires Windows")
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    left, top, native_width, native_height = virtual_screen()
    if native_width <= 0 or native_height <= 0:
        raise CaptureError("virtual screen has no area; is the session disconnected?")
    target_width, target_height = target_size(native_width, native_height, width)

    screen_dc = user32.GetDC(None)
    if not screen_dc:
        raise CaptureError("GetDC failed")
    memory_dc = bitmap = None
    try:
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        if not memory_dc:
            raise CaptureError("CreateCompatibleDC failed")
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, target_width, target_height)
        if not bitmap:
            raise CaptureError("CreateCompatibleBitmap failed")
        gdi32.SelectObject(memory_dc, bitmap)
        gdi32.SetStretchBltMode(memory_dc, HALFTONE)
        gdi32.SetBrushOrgEx(memory_dc, 0, 0, None)
        if not gdi32.StretchBlt(memory_dc, 0, 0, target_width, target_height,
                                screen_dc, left, top, native_width, native_height, SRCCOPY):
            raise CaptureError("StretchBlt failed")

        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = target_width
        # Negative height requests a top-down DIB, so rows arrive in PNG order.
        info.bmiHeader.biHeight = -target_height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB

        buffer = ctypes.create_string_buffer(target_width * target_height * 4)
        copied = gdi32.GetDIBits(memory_dc, bitmap, 0, target_height, buffer,
                                 ctypes.byref(info), DIB_RGB_COLORS)
        if copied != target_height:
            raise CaptureError(f"GetDIBits returned {copied} of {target_height} rows")
    finally:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)

    return bgra_to_rgb(buffer.raw), target_width, target_height, native_width, native_height
