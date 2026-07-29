import asyncio

import pytest

from rdpflux.control.client import ControlError
from rdpflux.control.http_client import HTTPControlClient, _split_base
from rdpflux.control.mcp import MCPServer

from tests.test_control_http import serve


def test_split_base_parses_host_and_port():
    assert _split_base("http://127.0.0.1:18080") == ("127.0.0.1", 18080)
    assert _split_base("127.0.0.1:9000") == ("127.0.0.1", 9000)


@pytest.mark.asyncio
async def test_http_client_screenshot_against_real_server():
    port, _, close = await serve(token="secret")
    try:
        client = HTTPControlClient(f"http://127.0.0.1:{port}", "secret")
        result, body = await client.screenshot(width=1280)
        assert result["format"] == "png"
        assert body.startswith(b"\x89PNG")
    finally:
        await close()


@pytest.mark.asyncio
async def test_http_client_action_against_real_server():
    port, backend, close = await serve(token="secret")
    try:
        client = HTTPControlClient(f"http://127.0.0.1:{port}", "secret")
        await client.request("action", {"action": "left_click", "coordinate": [3, 4]})
        assert backend.performed[-1].name == "left_click"
    finally:
        await close()


@pytest.mark.asyncio
async def test_http_client_surfaces_errors():
    port, _, close = await serve(token="secret")
    try:
        client = HTTPControlClient(f"http://127.0.0.1:{port}", "wrong-token")
        with pytest.raises(ControlError, match="HTTP 401"):
            await client.request("action", {"action": "left_click", "coordinate": [1, 2]})
    finally:
        await close()


@pytest.mark.asyncio
async def test_mcp_over_http_client_end_to_end():
    """MCP subprocess -> REST listener -> mux -> agent, the real deployment path."""
    port, backend, close = await serve(token="secret")
    try:
        client = HTTPControlClient(f"http://127.0.0.1:{port}", "secret")
        server = MCPServer(client)
        response = await server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "computer_action",
                       "arguments": {"action": "double_click", "coordinate": [7, 8]}},
        })
        assert "error" not in response
        assert backend.performed[-1].name == "double_click"
    finally:
        await close()
