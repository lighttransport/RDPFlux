import os

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="desktop control is Windows-only")

from rdpflux.control import capture, inject  # noqa: E402
from rdpflux.control.actions import ActionError  # noqa: E402


def test_virtual_screen_has_area():
    _, _, width, height = capture.virtual_screen()
    assert width > 0 and height > 0


def test_grab_matches_the_requested_geometry():
    rgb, width, height, native_width, native_height = capture.grab(640)
    assert width == 640
    assert height == round(640 * native_height / native_width)
    assert len(rgb) == width * height * 3


def test_grab_does_not_upscale():
    _, _, native_width, _ = capture.virtual_screen()
    _, width, _, _, _ = capture.grab(native_width + 500)
    assert width == native_width


@pytest.mark.parametrize("name, expected", [
    ("ctrl", 0x11), ("control", 0x11), ("shift", 0x10), ("alt", 0x12),
    ("super", 0x5B), ("win", 0x5B),
    ("return", 0x0D), ("Return", 0x0D), ("enter", 0x0D),
    ("escape", 0x1B), ("esc", 0x1B), ("tab", 0x09),
    ("f1", 0x70), ("F5", 0x74), ("f24", 0x87),
    ("page_down", 0x22), ("next", 0x22), ("left", 0x25), ("delete", 0x2E),
])
def test_resolve_key_named_keys(name, expected):
    assert inject.resolve_key(name)[0] == expected


def test_resolve_key_single_characters_use_the_active_layout():
    assert inject.resolve_key("a")[0] == 0x41
    assert inject.resolve_key("z")[0] == 0x5A


def test_resolve_key_rejects_nonsense():
    with pytest.raises(ActionError, match="unknown key"):
        inject.resolve_key("banana")


def test_absolute_maps_the_screen_corners_to_the_full_range():
    _, _, width, height = capture.virtual_screen()
    assert inject._absolute(0, 0) == (0, 0)
    assert inject._absolute(width - 1, height - 1) == (65535, 65535)


def test_absolute_clamps_out_of_range_points():
    _, _, width, height = capture.virtual_screen()
    assert inject._absolute(width * 2, height * 2) == (65535, 65535)
    assert inject._absolute(-500, -500) == (0, 0)


def test_cursor_position_round_trips():
    """The cursor must land exactly where it was told to go, or clicks miss."""
    _, _, width, height = capture.virtual_screen()
    original = inject.cursor_position()
    try:
        for target in [(0, 0), (width - 1, height - 1), (width // 2, height // 2)]:
            inject.move_to(*target)
            assert inject.cursor_position() == target
    finally:
        inject.move_to(*original)


def test_press_combination_rejects_an_empty_chord():
    with pytest.raises(ActionError, match="empty"):
        inject.press_combination("+")
