import asyncio

import pytest

from rdp2tcp.config import AgentConfig, ConfigError, Endpoint, parse_allowed_target, parse_endpoint
from rdp2tcp.policy import PolicyDenied, resolve_allowed


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
