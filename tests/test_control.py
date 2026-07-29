import asyncio
from typing import Any

import pytest

from rdpflux.config import AgentConfig
from rdpflux.control.actions import Action, ActionError, parse_action, scale_action
from rdpflux.control.client import ControlClient, ControlError
from rdpflux.control.framing import FramingError, MessageReader, encode_message
from rdpflux.control.service import ControlService, Screenshot
from rdpflux.forwarding import AgentForwarder
from rdpflux.mux import MuxPeer
from rdpflux.transport import MemoryTransport


class FakeBackend:
    """Records actions instead of touching a desktop."""

    def __init__(self, native=(2560, 1440)) -> None:
        self.native = native
        self.performed: list[Action] = []

    def native_size(self) -> tuple[int, int]:
        return self.native

    async def screenshot(self, *, width: int, image_format: str, quality: int) -> Screenshot:
        native_width, native_height = self.native
        height = round(width * native_height / native_width)
        return Screenshot(
            data=b"\x89PNG\r\n\x1a\n" + bytes(64),
            width=width,
            height=height,
            native_width=native_width,
            native_height=native_height,
            format=image_format,
        )

    async def perform(self, action: Action) -> dict[str, Any]:
        self.performed.append(action)
        return {"performed": action.name}


async def connect(config: AgentConfig | None = None, backend: FakeBackend | None = None):
    """Wire a client peer to an agent forwarder over an in-memory transport."""
    backend = backend or FakeBackend()
    left, right = MemoryTransport.pair()
    client_peer = MuxPeer(left, role="client")
    agent_peer = MuxPeer(right, role="agent")

    async def reject(stream, metadata):
        raise ValueError("client accepts no streams in this test")

    client_peer.set_handlers(on_open=reject)
    agent = AgentForwarder(agent_peer, config or AgentConfig(enable_control=True),
                           ControlService(backend))
    await asyncio.gather(agent.start(), client_peer.start())
    await client_peer.wait_ready()
    return ControlClient(client_peer), agent, backend


# --- action validation ---------------------------------------------------

def test_parse_action_accepts_a_click():
    action = parse_action({"action": "left_click", "coordinate": [10, 20]})
    assert action.name == "left_click"
    assert action.params["coordinate"] == (10, 20)


@pytest.mark.parametrize("payload, message", [
    ({"action": "nope"}, "unknown action"),
    ({"action": "left_click"}, "requires coordinate"),
    ({"action": "left_click", "coordinate": [1, 2], "duration": 1}, "does not accept duration"),
    ({"action": "left_click", "coordinate": [1]}, "two-element"),
    ({"action": "left_click", "coordinate": [1, -2]}, "must not be negative"),
    ({"action": "left_click", "coordinate": [1, True]}, "must contain integers"),
    ({"action": "type", "text": ""}, "must not be empty"),
    ({"action": "wait", "duration": 999}, "between 0 and"),
    ({"action": "scroll", "coordinate": [1, 2], "scroll_direction": "sideways",
      "scroll_amount": 1}, "scroll_direction must be"),
    ({"action": 5}, "action must be a string"),
])
def test_parse_action_rejects_malformed_input(payload, message):
    with pytest.raises(ActionError, match=message):
        parse_action(payload)


def test_scale_action_maps_both_coordinates():
    action = Action("left_click_drag", {"start_coordinate": (10, 10), "coordinate": (100, 50)})
    scaled = scale_action(action, 2.0, 2.0)
    assert scaled.params["start_coordinate"] == (20, 20)
    assert scaled.params["coordinate"] == (200, 100)


def test_scale_action_leaves_text_alone():
    action = Action("type", {"text": "hello"})
    assert scale_action(action, 2.0, 2.0).params == {"text": "hello"}


# --- framing -------------------------------------------------------------

class _FakeStream:
    """Feeds a fixed byte string to MessageReader in small pieces."""

    def __init__(self, data: bytes, chunk: int = 7) -> None:
        self.data = data
        self.chunk = chunk

    async def read(self, size: int = -1) -> bytes:
        piece, self.data = self.data[:self.chunk], self.data[self.chunk:]
        return piece


@pytest.mark.asyncio
async def test_framing_round_trip_across_chunk_boundaries():
    body = bytes(range(256)) * 40
    encoded = encode_message({"op": "screenshot"}, body)
    header, received = await MessageReader(_FakeStream(encoded)).read_message()
    assert header == {"op": "screenshot", "body_len": len(body)}
    assert received == body


@pytest.mark.asyncio
async def test_framing_reports_a_truncated_body():
    encoded = encode_message({"op": "screenshot"}, b"1234567890")
    with pytest.raises(FramingError, match="stream ended after"):
        await MessageReader(_FakeStream(encoded[:-4])).read_message()


@pytest.mark.asyncio
async def test_framing_returns_none_on_a_clean_close():
    assert await MessageReader(_FakeStream(b"")).read_message() is None


@pytest.mark.asyncio
async def test_framing_rejects_a_non_object_header():
    with pytest.raises(FramingError, match="must be a JSON object"):
        await MessageReader(_FakeStream(b'["not an object"]\n')).read_message()


# --- end to end over the mux --------------------------------------------

@pytest.mark.asyncio
async def test_screenshot_round_trip():
    client, agent, _ = await connect()
    try:
        result, body = await client.screenshot(width=1280)
        assert result["width"] == 1280
        assert result["height"] == 720
        assert result["native"] == [2560, 1440]
        assert body.startswith(b"\x89PNG")
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_action_coordinates_scale_to_native_pixels():
    client, agent, backend = await connect()
    try:
        await client.screenshot(width=1280)
        await client.act("left_click", coordinate=[640, 360])
        assert backend.performed[-1].params["coordinate"] == (1280, 720)
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_actions_are_unscaled_before_any_screenshot():
    """Without a delivered geometry the client is assumed to speak native pixels."""
    client, agent, backend = await connect()
    try:
        await client.act("mouse_move", coordinate=[640, 360])
        assert backend.performed[-1].params["coordinate"] == (640, 360)
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_invalid_action_is_reported_without_killing_the_mux():
    client, agent, backend = await connect()
    try:
        with pytest.raises(ControlError, match="unknown action"):
            await client.act("explode")
        # The connection survives, so a bad request costs one stream, not the tunnel.
        await client.act("left_click", coordinate=[1, 2])
        assert backend.performed[-1].name == "left_click"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_unknown_op_is_rejected():
    client, agent, _ = await connect()
    try:
        with pytest.raises(ControlError, match="unknown op"):
            await client.request("teleport")
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_control_is_refused_when_disabled():
    client, agent, _ = await connect(AgentConfig())
    try:
        with pytest.raises(ConnectionError, match="disabled"):
            await client.act("left_click", coordinate=[1, 2])
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_screenshot_width_is_bounded():
    client, agent, _ = await connect()
    try:
        with pytest.raises(ControlError, match="width must be between"):
            await client.screenshot(width=99999)
    finally:
        await agent.close()
