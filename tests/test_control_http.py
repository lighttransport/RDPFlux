import asyncio
import json

import pytest

from rdpflux.control.client import ControlClient
from rdpflux.control.http import ControlHTTPServer
from rdpflux.control.openapi import build_spec
from rdpflux.control.service import ControlService
from rdpflux.control import system
from rdpflux.forwarding import AgentForwarder
from rdpflux.mux import MuxPeer
from rdpflux.transport import MemoryTransport

from tests.test_control import FakeBackend


async def raw_request(port, method, path, *, body=b"", token=None, ctype="application/json"):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    headers = [f"{method} {path} HTTP/1.1", "Host: localhost", "Connection: close"]
    if token:
        headers.append(f"Authorization: Bearer {token}")
    if body:
        headers.append(f"Content-Type: {ctype}")
        headers.append(f"Content-Length: {len(body)}")
    request = ("\r\n".join(headers) + "\r\n\r\n").encode("latin-1") + body
    writer.write(request)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    resp_headers = {}
    for line in head.decode("latin-1").split("\r\n")[1:]:
        name, _, value = line.partition(":")
        resp_headers[name.strip().lower()] = value.strip()
    return status, resp_headers, payload


async def serve(token="secret", **service_kwargs):
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

    http = ControlHTTPServer(ControlClient(client_peer), token=token,
                             exec_enabled=service_kwargs.get("allow_exec", False),
                             files_enabled=service_kwargs.get("files") is not None,
                             system_enabled=service_kwargs.get("system_enabled", False),
                             clipboard_enabled=service_kwargs.get("clipboard_enabled", False))
    server = await http.start("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def close():
        await http.close()
        await agent.close()

    return port, backend, close


@pytest.mark.asyncio
async def test_openapi_spec_is_served_without_a_token():
    port, _, close = await serve()
    try:
        status, headers, payload = await raw_request(port, "GET", "/openapi.json")
        assert status == 200
        spec = json.loads(payload)
        assert spec["openapi"].startswith("3.")
        assert "/v1/screenshot" in spec["paths"]
        assert "/v1/action" in spec["paths"]
    finally:
        await close()


@pytest.mark.asyncio
async def test_screenshot_returns_an_image():
    port, _, close = await serve()
    try:
        body = json.dumps({"width": 1280, "format": "png"}).encode()
        status, headers, payload = await raw_request(port, "POST", "/v1/screenshot",
                                                     body=body, token="secret")
        assert status == 200
        assert headers["content-type"] == "image/png"
        assert payload.startswith(b"\x89PNG")
    finally:
        await close()


@pytest.mark.asyncio
async def test_action_is_forwarded():
    port, backend, close = await serve()
    try:
        body = json.dumps({"action": "left_click", "coordinate": [10, 20]}).encode()
        status, _, payload = await raw_request(port, "POST", "/v1/action",
                                               body=body, token="secret")
        assert status == 200
        assert backend.performed[-1].name == "left_click"
    finally:
        await close()


@pytest.mark.asyncio
async def test_missing_token_is_rejected():
    port, _, close = await serve()
    try:
        body = json.dumps({"action": "left_click", "coordinate": [1, 2]}).encode()
        status, _, payload = await raw_request(port, "POST", "/v1/action", body=body)
        assert status == 401
        assert b"token" in payload
    finally:
        await close()


@pytest.mark.asyncio
async def test_bad_token_is_rejected():
    port, _, close = await serve()
    try:
        status, _, _ = await raw_request(port, "POST", "/v1/action",
                                         body=b"{}", token="wrong")
        assert status == 401
    finally:
        await close()


@pytest.mark.asyncio
async def test_invalid_action_surfaces_as_bad_gateway():
    port, _, close = await serve()
    try:
        body = json.dumps({"action": "explode"}).encode()
        status, _, payload = await raw_request(port, "POST", "/v1/action",
                                               body=body, token="secret")
        assert status == 502
        assert b"unknown action" in payload
    finally:
        await close()


@pytest.mark.asyncio
async def test_system_processes_route_is_forwarded(monkeypatch):
    async def fake_process_list():
        return {"processes": [{"Id": 123, "ProcessName": "demo"}]}

    monkeypatch.setattr(system, "process_list", fake_process_list)
    port, _, close = await serve(system_enabled=True)
    try:
        status, headers, payload = await raw_request(
            port, "GET", "/v1/system/processes", token="secret")
        assert status == 200
        assert headers["content-type"] == "application/json"
        assert json.loads(payload) == {"processes": [{"Id": 123, "ProcessName": "demo"}]}
    finally:
        await close()


@pytest.mark.asyncio
async def test_unknown_route_is_404():
    port, _, close = await serve()
    try:
        status, _, _ = await raw_request(port, "GET", "/nope", token="secret")
        assert status == 404
    finally:
        await close()


@pytest.mark.asyncio
async def test_file_round_trip_over_http(tmp_path):
    from rdpflux.control.files import FileStore
    port, _, close = await serve(files=FileStore(tmp_path))
    try:
        status, _, result = await raw_request(
            port, "PUT", "/v1/file?path=out.bin", body=b"binary\x00data",
            token="secret", ctype="application/octet-stream")
        assert status == 200
        status, headers, payload = await raw_request(
            port, "GET", "/v1/file?path=out.bin", token="secret")
        assert status == 200
        assert headers["content-type"] == "application/octet-stream"
        assert payload == b"binary\x00data"
    finally:
        await close()


def test_openapi_spec_gates_optional_ops():
    minimal = build_spec(exec_enabled=False, files_enabled=False)
    assert "/v1/exec" not in minimal["paths"]
    assert "/v1/file" not in minimal["paths"]
    full = build_spec(exec_enabled=True, files_enabled=True)
    assert "/v1/exec" in full["paths"]
    assert "/v1/file" in full["paths"]
    system = build_spec(exec_enabled=False, files_enabled=False, system_enabled=True,
                        clipboard_enabled=True)
    assert "/v1/system/processes" in system["paths"]
    assert "/v1/system/services/control" in system["paths"]
    assert "/v1/clipboard" in system["paths"]
    # The action schema enumerates every action as a oneOf variant.
    variants = full["paths"]["/v1/action"]["post"]["requestBody"]["content"]["application/json"]["schema"]["oneOf"]
    names = {v["properties"]["action"]["const"] for v in variants}
    assert {"left_click", "type", "scroll", "screenshot"} <= names
