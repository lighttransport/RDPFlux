from __future__ import annotations

import asyncio
import ipaddress
import socket

from .config import AgentConfig, Endpoint


class PolicyDenied(Exception):
    pass


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def resolve_allowed(endpoint: Endpoint, config: AgentConfig) -> list[tuple[int, str]]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(endpoint.host, endpoint.port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise PolicyDenied(f"cannot resolve {endpoint.host}: {exc}") from exc
    allowed: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for family, _type, _proto, _canon, sockaddr in infos:
        address = sockaddr[0]
        item = (family, address)
        if item in seen:
            continue
        seen.add(item)
        if any(rule.permits(address, endpoint.port) for rule in config.allow_targets):
            allowed.append(item)
    if not allowed:
        raise PolicyDenied(f"destination {endpoint} is not allowed")
    return allowed


def validate_reverse_listener(endpoint: Endpoint, config: AgentConfig) -> None:
    if not config.enable_reverse:
        raise PolicyDenied("reverse forwarding is disabled")
    if not is_loopback_host(endpoint.host) and not config.allow_nonloopback_reverse:
        raise PolicyDenied("non-loopback reverse listeners are disabled")
