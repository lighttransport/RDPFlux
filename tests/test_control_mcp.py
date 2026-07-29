import asyncio
import base64

import pytest

from rdpflux.control.client import ControlClient
from rdpflux.control.mcp import MCPServer, tool_definitions
from rdpflux.control.service import ControlService
from rdpflux.forwarding import AgentForwarder
from rdpflux.mux import MuxPeer
from rdpflux.transport import MemoryTransport

from tests.test_control import FakeBackend


async def make_server(**service_kwargs):
    backend = FakeBackend()
    left, right = MemoryTransport.pair()
    client_peer = MuxPeer(left, role="client")
    agent_peer = MuxPeer(right, role="agent")

    async def reject(stream, metadata):
        raise ValueError("no streams")

    client_peer.set_handlers(on_open=reject)
    from rdpflux.config import AgentConfig
    agent = AgentForwarder(agent_peer, AgentConfig(enable_control=True),
                           ControlService(backend, **service_kwargs))
    await asyncio.gather(agent.start(), client_peer.start())
    await client_peer.wait_ready()
    server = MCPServer(ControlClient(client_peer),
                       exec_enabled=service_kwargs.get("allow_exec", False),
                       files_enabled=service_kwargs.get("files") is not None)
    return server, backend, agent


def test_tool_definitions_gate_optional_tools():
    names = {t["name"] for t in tool_definitions(exec_enabled=False, files_enabled=False)}
    assert names == {"screenshot", "computer_action"}
    full = {t["name"] for t in tool_definitions(exec_enabled=True, files_enabled=True)}
    assert {"run_command", "read_file", "write_file", "list_dir"} <= full


@pytest.mark.asyncio
async def test_initialize_and_list_tools():
    server, _, agent = await make_server()
    try:
        init = await server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert init["result"]["serverInfo"]["name"] == "rdpflux-control"
        listed = await server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in listed["result"]["tools"]}
        assert "screenshot" in names and "computer_action" in names
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_screenshot_tool_returns_a_base64_image():
    server, _, agent = await make_server()
    try:
        response = await server.dispatch({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "screenshot", "arguments": {"width": 1280}},
        })
        block = response["result"]["content"][0]
        assert block["type"] == "image"
        assert block["mimeType"] == "image/png"
        assert base64.b64decode(block["data"]).startswith(b"\x89PNG")
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_computer_action_tool_is_forwarded():
    server, backend, agent = await make_server()
    try:
        await server.dispatch({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "computer_action",
                       "arguments": {"action": "left_click", "coordinate": [5, 6]}},
        })
        assert backend.performed[-1].name == "left_click"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_invalid_action_becomes_a_json_rpc_error():
    server, _, agent = await make_server()
    try:
        response = await server.dispatch({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "computer_action", "arguments": {"action": "explode"}},
        })
        assert response["error"]["code"] == -32000
        assert "unknown action" in response["error"]["message"]
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_notification_gets_no_reply():
    server, _, agent = await make_server()
    try:
        assert await server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_unknown_method_is_reported():
    server, _, agent = await make_server()
    try:
        response = await server.dispatch({"jsonrpc": "2.0", "id": 6, "method": "frobnicate"})
        assert response["error"]["code"] == -32601
    finally:
        await agent.close()
