from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The action names mirror Anthropic's computer-use tool so a model can drive this
# surface with no adapter layer, and so the REST/OpenAPI surface uses a vocabulary
# generic clients already have examples for.
#
# The per-action parameter shapes below still need checking against the live
# computer-use documentation before the MCP and OpenAPI adapters ship; the doc
# pages returned 404 while this was written.

MAX_TEXT = 4096
MAX_DURATION = 60.0
MAX_SCROLL_AMOUNT = 100
SCROLL_DIRECTIONS = ("up", "down", "left", "right")


class ActionError(ValueError):
    """The requested action or its parameters are not valid."""


@dataclass(frozen=True, slots=True)
class ActionSpec:
    name: str
    description: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()

    @property
    def accepted(self) -> tuple[str, ...]:
        return self.required + self.optional


def _click(name: str, description: str) -> ActionSpec:
    # `text` carries modifier keys held during the click, e.g. "ctrl+shift".
    return ActionSpec(name, description, required=("coordinate",), optional=("text",))


ACTIONS: dict[str, ActionSpec] = {
    spec.name: spec
    for spec in (
        ActionSpec("screenshot", "Capture the remote desktop."),
        ActionSpec("cursor_position", "Return the current cursor coordinate."),
        ActionSpec("mouse_move", "Move the cursor.", required=("coordinate",)),
        _click("left_click", "Click the left mouse button."),
        _click("right_click", "Click the right mouse button."),
        _click("middle_click", "Click the middle mouse button."),
        _click("double_click", "Double-click the left mouse button."),
        _click("triple_click", "Triple-click the left mouse button."),
        ActionSpec("left_mouse_down", "Press and hold the left mouse button.", optional=("coordinate",)),
        ActionSpec("left_mouse_up", "Release the left mouse button.", optional=("coordinate",)),
        ActionSpec("left_click_drag", "Drag with the left button held.",
                   required=("start_coordinate", "coordinate")),
        ActionSpec("scroll", "Scroll the wheel at a coordinate.",
                   required=("coordinate", "scroll_direction", "scroll_amount"), optional=("text",)),
        ActionSpec("key", "Press a key combination, e.g. 'ctrl+s' or 'Return'.", required=("text",)),
        ActionSpec("type", "Type literal text.", required=("text",)),
        ActionSpec("hold_key", "Hold a key for a duration in seconds.", required=("text", "duration")),
        ActionSpec("wait", "Wait for a duration in seconds.", required=("duration",)),
    )
}


@dataclass(frozen=True, slots=True)
class Action:
    """A validated action. Coordinates are in screenshot pixel space."""

    name: str
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def spec(self) -> ActionSpec:
        return ACTIONS[self.name]


def _coordinate(value: Any, label: str) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ActionError(f"{label} must be a two-element [x, y] array")
    # bool is an int subclass, and a coordinate of `true` is a caller bug worth naming.
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ActionError(f"{label} must contain integers")
    if any(item < 0 for item in value):
        raise ActionError(f"{label} must not be negative")
    return int(value[0]), int(value[1])


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ActionError(f"{label} must be a string")
    if not value:
        raise ActionError(f"{label} must not be empty")
    if len(value) > MAX_TEXT:
        raise ActionError(f"{label} exceeds {MAX_TEXT} characters")
    return value


def _duration(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ActionError("duration must be a number")
    if not 0 <= value <= MAX_DURATION:
        raise ActionError(f"duration must be between 0 and {MAX_DURATION} seconds")
    return float(value)


def _scroll_amount(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ActionError("scroll_amount must be an integer")
    if not 0 < value <= MAX_SCROLL_AMOUNT:
        raise ActionError(f"scroll_amount must be between 1 and {MAX_SCROLL_AMOUNT}")
    return value


def _scroll_direction(value: Any) -> str:
    if value not in SCROLL_DIRECTIONS:
        raise ActionError(f"scroll_direction must be one of {', '.join(SCROLL_DIRECTIONS)}")
    return value


_VALIDATORS = {
    "coordinate": lambda v: _coordinate(v, "coordinate"),
    "start_coordinate": lambda v: _coordinate(v, "start_coordinate"),
    "text": lambda v: _text(v, "text"),
    "duration": _duration,
    "scroll_amount": _scroll_amount,
    "scroll_direction": _scroll_direction,
}


def parse_action(payload: dict[str, Any]) -> Action:
    """Validate an action request, raising ActionError on anything malformed."""
    name = payload.get("action")
    if not isinstance(name, str):
        raise ActionError("action must be a string")
    spec = ACTIONS.get(name)
    if spec is None:
        raise ActionError(f"unknown action {name}")

    supplied = {key: value for key, value in payload.items() if key != "action"}
    unknown = sorted(set(supplied) - set(spec.accepted))
    if unknown:
        raise ActionError(f"{name} does not accept {', '.join(unknown)}")
    missing = sorted(set(spec.required) - set(supplied))
    if missing:
        raise ActionError(f"{name} requires {', '.join(missing)}")

    return Action(name, {key: _VALIDATORS[key](value) for key, value in supplied.items()})


def scale_action(action: Action, scale_x: float, scale_y: float) -> Action:
    """Map coordinates from screenshot pixel space into native display pixels.

    The agent downscales captures before sending them, so the model works in the
    delivered image's coordinate space. Rescaling here keeps that the only space
    the client and the model ever see.
    """
    if scale_x == 1.0 and scale_y == 1.0:
        return action
    params = dict(action.params)
    for key in ("coordinate", "start_coordinate"):
        if key in params:
            x, y = params[key]
            params[key] = (round(x * scale_x), round(y * scale_y))
    return Action(action.name, params)
