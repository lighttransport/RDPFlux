import asyncio

import pytest

from rdpflux.config import (
    AgentConfig,
    ConfigError,
    Endpoint,
    load_agent_config,
    load_client_config,
    parse_allowed_target,
    parse_endpoint,
)
from rdpflux.policy import PolicyDenied, resolve_allowed


def test_endpoint_ipv4_ipv6_and_default():
    assert parse_endpoint("127.0.0.1:22") == Endpoint("127.0.0.1", 22)
    assert parse_endpoint("[::1]:443") == Endpoint("::1", 443)
    assert parse_endpoint("1080", default_host="127.0.0.1") == Endpoint("127.0.0.1", 1080)
    with pytest.raises(ConfigError):
        parse_endpoint("localhost:70000")


def test_allowed_target_parser():
    rule = parse_allowed_target("10.0.0.0/8:22-23")
    assert rule.permits("10.1.2.3", 22)
    assert not rule.permits("10.1.2.3", 80)
    assert parse_allowed_target("::1/128:*").permits("::1", 65535)


@pytest.mark.asyncio
async def test_default_policy_allows_loopback_and_denies_external():
    cfg = AgentConfig()
    assert await resolve_allowed(Endpoint("127.0.0.1", 22), cfg)
    with pytest.raises(PolicyDenied):
        await resolve_allowed(Endpoint("192.0.2.1", 22), cfg)


def test_configuration_rejects_coerced_and_non_finite_limits(tmp_path):
    client = tmp_path / "client.json"
    client.write_text('{"limits":{"max_streams":true}}', encoding="utf-8")
    with pytest.raises(ConfigError, match="integer"):
        load_client_config(client)
    client.write_text('{"limits":{"connect_timeout":"15"}}', encoding="utf-8")
    with pytest.raises(ConfigError, match="number"):
        load_client_config(client)

    agent = tmp_path / "agent.json"
    agent.write_text('{"enable_reverse":"false"}', encoding="utf-8")
    with pytest.raises(ConfigError, match="boolean"):
        load_agent_config(agent)


def test_configuration_rejects_malformed_collections(tmp_path):
    client = tmp_path / "client.json"
    client.write_text('{"socks":{},"local_forwards":{}}', encoding="utf-8")
    with pytest.raises(ConfigError, match="array"):
        load_client_config(client)
    with pytest.raises(ConfigError, match="port range"):
        parse_allowed_target("127.0.0.1:nope")


def test_sync_forwards_are_loaded_separately(tmp_path):
    client = tmp_path / "client.json"
    client.write_text(
        '{"sync_forwards":[{"name":"mutagen","listen":"127.0.0.1:2223",'
        '"target":"127.0.0.1:22"}]}', encoding="utf-8")
    cfg = load_client_config(client)
    assert cfg.local_forwards == []
    assert len(cfg.sync_forwards) == 1
    assert cfg.sync_forwards[0].name == "mutagen"


def test_proxy_forwards_are_loaded_separately(tmp_path):
    client = tmp_path / "client.json"
    client.write_text(
        '{"proxy_forwards":[{"name":"linux-service","listen":"127.0.0.1:9000",'
        '"target":"192.168.1.20:9000"}]}', encoding="utf-8")
    cfg = load_client_config(client)
    assert cfg.local_forwards == []
    assert cfg.sync_forwards == []
    assert cfg.proxy_forwards[0].target == Endpoint("192.168.1.20", 9000)


def test_control_capabilities_are_loaded_and_validated(tmp_path):
    client = tmp_path / "client.json"
    client.write_text(
        '{"control":{"system_ops":true,"clipboard":true}}', encoding="utf-8")
    cfg = load_client_config(client)
    assert cfg.control_system_ops is True
    assert cfg.control_clipboard is True

    agent = tmp_path / "agent.json"
    agent.write_text(
        '{"enable_system_ops":true,"enable_clipboard":true,'
        '"allow_process_terminate":true,'
        '"system_service_allowlist":["Spooler"],'
        '"system_task_allowlist":["Demo"]}', encoding="utf-8")
    cfg = load_agent_config(agent)
    assert cfg.enable_system_ops is True
    assert cfg.enable_clipboard is True
    assert cfg.allow_process_terminate is True
    assert cfg.system_service_allowlist == ["Spooler"]
    assert cfg.system_task_allowlist == ["Demo"]

    agent.write_text('{"system_service_allowlist":"Spooler"}', encoding="utf-8")
    with pytest.raises(ConfigError, match="array"):
        load_agent_config(agent)


def test_file_access_policy_is_loaded(tmp_path):
    agent = tmp_path / "agent.json"
    agent.write_text(
        '{"enable_file_transfer":true,"file_allowlist":['
        '{"pattern":"safe/**","mode":"read"},'
        '{"pattern":"out/**","mode":"write"}],'
        '"file_denylist":["safe/private/**"]}', encoding="utf-8")
    cfg = load_agent_config(agent)
    assert cfg.max_file_upload == 128 * 1024 * 1024
    assert [(rule.pattern, rule.mode) for rule in cfg.file_allowlist] == [
        ("safe/**", "read"), ("out/**", "write")]
    assert cfg.file_denylist == ["safe/private/**"]

    agent.write_text('{"file_allowlist":[{"pattern":"**","mode":"delete"}]}', encoding="utf-8")
    with pytest.raises(ConfigError, match="mode"):
        load_agent_config(agent)

    agent.write_text('{"max_file_upload":1048576}', encoding="utf-8")
    assert load_agent_config(agent).max_file_upload == 1048576
    agent.write_text('{"max_file_upload":0}', encoding="utf-8")
    with pytest.raises(ConfigError, match="max_file_upload"):
        load_agent_config(agent)


def test_multiple_file_roots_are_loaded(tmp_path):
    agent = tmp_path / "agent.json"
    agent.write_text(
        '{"enable_file_transfer":true,"file_roots":['
        '{"name":"temp","path":"D:\\\\temp",'
        '"allowlist":[{"pattern":"**","mode":"read_write"}]},'
        '{"name":"reports","path":"D:\\\\reports",'
        '"allowlist":[{"pattern":"**","mode":"read"}],'
        '"denylist":["private/**"]}]}', encoding="utf-8")
    cfg = load_agent_config(agent)
    assert [(root.name, root.path) for root in cfg.file_roots] == [
        ("temp", "D:\\temp"), ("reports", "D:\\reports")]
    assert cfg.file_roots[1].allowlist[0].mode == "read"
    assert cfg.file_roots[1].denylist == ["private/**"]

    agent.write_text(
        '{"file_roots":[{"name":"bad/name","path":"D:\\\\temp"}]}',
        encoding="utf-8")
    with pytest.raises(ConfigError, match="root names"):
        load_agent_config(agent)
