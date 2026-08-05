from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import os
from typing import Any

from .actions import ActionError

MAX_CLIPBOARD_TEXT = 4 * 1024 * 1024


def _require_windows() -> None:
    if os.name != "nt":
        raise ActionError("clipboard control is only available on Windows")


def _apis():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    return user32, kernel32


def _read_text() -> str:
    _require_windows()
    user32, kernel32 = _apis()
    CF_UNICODETEXT = 13
    if not user32.OpenClipboard(None):
        raise ActionError("cannot open the Windows clipboard")
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise ActionError("cannot lock clipboard data")
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _write_text(value: str) -> dict[str, Any]:
    _require_windows()
    if not isinstance(value, str):
        raise ActionError("clipboard text must be a string")
    if len(value.encode("utf-8")) > MAX_CLIPBOARD_TEXT:
        raise ActionError(f"clipboard text exceeds {MAX_CLIPBOARD_TEXT} bytes")
    user32, kernel32 = _apis()
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    encoded = ctypes.create_unicode_buffer(value)
    size = ctypes.sizeof(encoded)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        raise ActionError("cannot allocate clipboard memory")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        raise ActionError("cannot lock clipboard memory")
    try:
        ctypes.memmove(pointer, ctypes.addressof(encoded), size)
    finally:
        kernel32.GlobalUnlock(handle)
    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        raise ActionError("cannot open the Windows clipboard")
    try:
        if not user32.EmptyClipboard() or not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise ActionError("cannot set the Windows clipboard")
        handle = None
    finally:
        user32.CloseClipboard()
    return {"bytes": len(value.encode("utf-8")), "characters": len(value)}


async def read_text() -> dict[str, Any]:
    value = await asyncio.to_thread(_read_text)
    return {"text": value, "characters": len(value)}


async def write_text(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        raise ActionError("clipboard text must be a string")
    return await asyncio.to_thread(_write_text, value)
