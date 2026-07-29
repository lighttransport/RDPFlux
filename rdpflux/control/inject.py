from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

from .actions import Action, ActionError
from .capture import virtual_screen

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WHEEL_DELTA = 120
CLICK_INTERVAL = 0.02

_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

_MODIFIERS = {
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "shift": 0x10,
    "super": 0x5B, "win": 0x5B, "cmd": 0x5B, "meta": 0x5B,
}

# Names follow the xdotool spelling that computer-use models emit, with the
# common friendly aliases accepted alongside.
_KEYS = {
    "return": 0x0D, "enter": 0x0D, "kp_enter": 0x0D,
    "tab": 0x09, "escape": 0x1B, "esc": 0x1B, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "prior": 0x21, "page_up": 0x21,
    "next": 0x22, "page_down": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "print": 0x2C, "printscreen": 0x2C, "pause": 0x13,
    "caps_lock": 0x14, "num_lock": 0x90, "scroll_lock": 0x91,
    "menu": 0x5D, "apps": 0x5D,
    **{f"f{index}": 0x6F + index for index in range(1, 25)},
}

# Keys on the extended scan-code page; without the flag Windows can confuse the
# arrow cluster with the numeric keypad.
_EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x5B, 0x5D, 0x90}

ULONG_PTR = wintypes.WPARAM


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long), ("dy", ctypes.c_long),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


class InjectError(OSError):
    pass


def _send(*events: INPUT) -> None:
    if os.name != "nt":
        raise InjectError("input injection requires Windows")
    array = (INPUT * len(events))(*events)
    sent = ctypes.windll.user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    if sent != len(events):
        error = ctypes.get_last_error()
        # UIPI blocks a non-elevated process from driving an elevated window, and
        # nothing can reach the UAC secure desktop or the lock screen.
        raise InjectError(error, f"SendInput delivered {sent} of {len(events)} events")


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    event = INPUT(type=INPUT_MOUSE)
    event.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF, dwFlags=flags,
                          time=0, dwExtraInfo=0)
    return event


def _key(vk: int, up: bool = False) -> INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in _EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    event = INPUT(type=INPUT_KEYBOARD)
    event.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
    return event


def _unicode(code: int, up: bool = False) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    event = INPUT(type=INPUT_KEYBOARD)
    event.ki = KEYBDINPUT(wVk=0, wScan=code, dwFlags=flags, time=0, dwExtraInfo=0)
    return event


def _absolute(x: int, y: int) -> tuple[int, int]:
    """Map native desktop pixels onto SendInput's 0..65535 virtual-desktop space."""
    left, top, width, height = virtual_screen()
    if width <= 1 or height <= 1:
        raise InjectError("virtual screen has no area")
    dx = round((x - left) * 65535 / (width - 1))
    dy = round((y - top) * 65535 / (height - 1))
    return max(0, min(65535, dx)), max(0, min(65535, dy))


def move_to(x: int, y: int) -> None:
    dx, dy = _absolute(x, y)
    _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, dx, dy))


def cursor_position() -> tuple[int, int]:
    point = wintypes.POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        raise InjectError(ctypes.get_last_error(), "GetCursorPos failed")
    return point.x, point.y


def resolve_key(name: str) -> tuple[int, bool]:
    """Return (virtual key code, needs shift) for one key name."""
    lowered = name.lower()
    if lowered in _MODIFIERS:
        return _MODIFIERS[lowered], False
    if lowered in _KEYS:
        return _KEYS[lowered], False
    if len(name) == 1:
        # VkKeyScanW honours the active layout, so 'z' works on AZERTY too.
        scan = ctypes.windll.user32.VkKeyScanW(ctypes.c_wchar(name))
        if scan != -1:
            return scan & 0xFF, bool(scan >> 8 & 1)
    raise ActionError(f"unknown key {name!r}")


def press_combination(combo: str) -> None:
    """Press a chord such as 'ctrl+shift+s', releasing in reverse order."""
    parts = [part for part in combo.replace(" ", "").split("+") if part]
    if not parts:
        raise ActionError("key combination is empty")
    codes: list[int] = []
    for part in parts:
        vk, needs_shift = resolve_key(part)
        if needs_shift and _MODIFIERS["shift"] not in codes:
            codes.append(_MODIFIERS["shift"])
        codes.append(vk)
    _send(*(_key(vk) for vk in codes), *(_key(vk, up=True) for vk in reversed(codes)))


def type_text(text: str) -> None:
    """Send literal text as Unicode, bypassing the keyboard layout entirely."""
    events: list[INPUT] = []
    for character in text:
        for code in _surrogates(character):
            events.append(_unicode(code))
            events.append(_unicode(code, up=True))
    # SendInput takes a bounded array; chunk so a long paste cannot overflow it.
    for start in range(0, len(events), 512):
        _send(*events[start:start + 512])


def _surrogates(character: str) -> tuple[int, ...]:
    code = ord(character)
    if code <= 0xFFFF:
        return (code,)
    code -= 0x10000
    return (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF))


def _with_modifiers(modifiers: str | None, body) -> None:
    codes = []
    if modifiers:
        for part in modifiers.replace(" ", "").split("+"):
            if part:
                codes.append(resolve_key(part)[0])
    for vk in codes:
        _send(_key(vk))
    try:
        body()
    finally:
        for vk in reversed(codes):
            _send(_key(vk, up=True))


def click(button: str, count: int = 1) -> None:
    down, up = _BUTTONS[button]
    for index in range(count):
        if index:
            time.sleep(CLICK_INTERVAL)
        _send(_mouse(down), _mouse(up))


def scroll(direction: str, amount: int) -> None:
    if direction in ("up", "down"):
        flags, delta = MOUSEEVENTF_WHEEL, WHEEL_DELTA * (1 if direction == "up" else -1)
    else:
        flags, delta = MOUSEEVENTF_HWHEEL, WHEEL_DELTA * (1 if direction == "right" else -1)
    for index in range(amount):
        if index:
            time.sleep(CLICK_INTERVAL)
        _send(_mouse(flags, data=delta))


def perform(action: Action) -> dict:
    """Execute one validated action. Coordinates are native desktop pixels."""
    name, params = action.name, action.params
    coordinate = params.get("coordinate")

    if name == "cursor_position":
        x, y = cursor_position()
        return {"coordinate": [x, y]}
    if name == "mouse_move":
        move_to(*coordinate)
        return {}
    if name in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
        move_to(*coordinate)
        button = {"left_click": "left", "right_click": "right", "middle_click": "middle"}.get(name, "left")
        count = {"double_click": 2, "triple_click": 3}.get(name, 1)
        _with_modifiers(params.get("text"), lambda: click(button, count))
        return {}
    if name == "left_mouse_down":
        if coordinate:
            move_to(*coordinate)
        _send(_mouse(MOUSEEVENTF_LEFTDOWN))
        return {}
    if name == "left_mouse_up":
        if coordinate:
            move_to(*coordinate)
        _send(_mouse(MOUSEEVENTF_LEFTUP))
        return {}
    if name == "left_click_drag":
        move_to(*params["start_coordinate"])
        _send(_mouse(MOUSEEVENTF_LEFTDOWN))
        try:
            move_to(*coordinate)
        finally:
            _send(_mouse(MOUSEEVENTF_LEFTUP))
        return {}
    if name == "scroll":
        move_to(*coordinate)
        _with_modifiers(
            params.get("text"),
            lambda: scroll(params["scroll_direction"], params["scroll_amount"]),
        )
        return {}
    if name == "key":
        press_combination(params["text"])
        return {}
    if name == "type":
        type_text(params["text"])
        return {}
    if name == "hold_key":
        vk = resolve_key(params["text"])[0]
        _send(_key(vk))
        try:
            time.sleep(params["duration"])
        finally:
            _send(_key(vk, up=True))
        return {}
    if name == "wait":
        time.sleep(params["duration"])
        return {}
    raise ActionError(f"action {name} is not implemented")
