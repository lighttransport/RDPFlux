from __future__ import annotations

import ipaddress
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class Endpoint:
    host: str
    port: int

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


def parse_endpoint(value: str, *, default_host: str | None = None) -> Endpoint:
    if not isinstance(value, str):
        raise ConfigError("endpoint must be a string")
    value = value.strip()
    if value.startswith("["):
        end = value.find("]")
        if end < 0 or end + 1 >= len(value) or value[end + 1] != ":":
            raise ConfigError(f"invalid endpoint: {value}")
        host, raw_port = value[1:end], value[end + 2:]
    elif ":" in value:
        host, raw_port = value.rsplit(":", 1)
    elif default_host is not None:
        host, raw_port = default_host, value
    else:
        raise ConfigError(f"endpoint requires host and port: {value}")
    host = host.strip()
    raw_port = raw_port.strip()
    if not host:
        if default_host is None:
            raise ConfigError(f"endpoint has an empty host: {value}")
        host = default_host
    if any(ord(character) < 32 or ord(character) == 127 for character in host):
        raise ConfigError("endpoint host contains control characters")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ConfigError(f"invalid port in endpoint: {value}") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"port is outside 1..65535: {port}")
    return Endpoint(host, port)


@dataclass(slots=True, frozen=True)
class ForwardRule:
    listen: Endpoint
    target: Endpoint
    name: str = ""


@dataclass(slots=True)
class ClientConfig:
    local_forwards: list[ForwardRule] = field(default_factory=list)
    # Convenience namespace for file synchronization tools such as Mutagen.
    # These are TCP forwards, just like local_forwards, but are kept separate
    # so that a configuration documents which listeners are sync-related.
    sync_forwards: list[ForwardRule] = field(default_factory=list)
    # Direct TCP proxies from this client machine to another private-network
    # endpoint. These do not cross the RDP mux.
    proxy_forwards: list[ForwardRule] = field(default_factory=list)
    socks: list[Endpoint] = field(default_factory=list)
    reverse_forwards: list[ForwardRule] = field(default_factory=list)
    # When set, expose the remote desktop-control service as a loopback REST API.
    # The token guards it, since any local process can reach a loopback listener.
    control_listen: Endpoint | None = None
    control_token: str = ""
    max_streams: int = 128
    connect_timeout: float = 15.0
    idle_timeout: float = 0.0


@dataclass(slots=True, frozen=True)
class AllowedTarget:
    network: ipaddress.IPv4Network | ipaddress.IPv6Network
    first_port: int = 1
    last_port: int = 65535

    def permits(self, address: str, port: int) -> bool:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return ip in self.network and self.first_port <= port <= self.last_port


@dataclass(slots=True)
class AgentConfig:
    allow_targets: list[AllowedTarget] = field(default_factory=lambda: [
        AllowedTarget(ipaddress.ip_network("127.0.0.0/8")),
        AllowedTarget(ipaddress.ip_network("::1/128")),
    ])
    enable_reverse: bool = False
    allow_nonloopback_reverse: bool = False
    # Desktop control is opt-in like reverse forwarding. Shell execution and file
    # transfer are separate flags because they turn desktop control into arbitrary
    # remote code execution, so they should not ride along with screenshots.
    enable_control: bool = False
    enable_exec: bool = False
    enable_file_transfer: bool = False
    file_root: str = ""
    max_streams: int = 128
    connect_timeout: float = 15.0


def _load_json(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError("configuration root must be an object")
    return value


def _rule(value: Any, label: str) -> ForwardRule:
    if not isinstance(value, dict) or not isinstance(value.get("listen"), str) or not isinstance(value.get("target"), str):
        raise ConfigError(f"{label} entries require string listen and target fields")
    name = value.get("name", "")
    if not isinstance(name, str):
        raise ConfigError(f"{label} entry names must be strings")
    return ForwardRule(parse_endpoint(value["listen"]), parse_endpoint(value["target"]), name)


def _array(raw: dict[str, Any], name: str) -> list[Any]:
    value = raw.get(name, [])
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be an array")
    return value


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _number(value: Any, name: str, *, minimum: float, maximum: float | None = None,
            allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(f"{name} must be finite")
    if result < minimum or (result == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero and minimum == 0 else f"greater than {minimum}"
        raise ConfigError(f"{name} must be {qualifier}")
    if maximum is not None and result > maximum:
        raise ConfigError(f"{name} must not exceed {maximum}")
    return result


def _boolean(raw: dict[str, Any], name: str, default: bool) -> bool:
    value = raw.get(name, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be a boolean")
    return value


def load_client_config(path: str | Path | None) -> ClientConfig:
    raw = _load_json(path)
    cfg = ClientConfig()
    cfg.local_forwards = [_rule(v, "local_forwards") for v in _array(raw, "local_forwards")]
    cfg.sync_forwards = [_rule(v, "sync_forwards") for v in _array(raw, "sync_forwards")]
    cfg.proxy_forwards = [_rule(v, "proxy_forwards") for v in _array(raw, "proxy_forwards")]
    cfg.reverse_forwards = [_rule(v, "reverse_forwards") for v in _array(raw, "reverse_forwards")]
    cfg.socks = []
    for value in _array(raw, "socks"):
        if isinstance(value, str):
            listen = value
        elif isinstance(value, dict) and isinstance(value.get("listen"), str):
            listen = value["listen"]
        else:
            raise ConfigError("socks entries must be endpoint strings or objects with a string listen field")
        cfg.socks.append(parse_endpoint(listen))
    control = raw.get("control")
    if isinstance(control, dict):
        listen = control.get("listen")
        if isinstance(listen, str) and listen:
            cfg.control_listen = parse_endpoint(listen, default_host="127.0.0.1")
        token = control.get("token", "")
        if not isinstance(token, str):
            raise ConfigError("control.token must be a string")
        cfg.control_token = token
    elif control is not None:
        raise ConfigError("control must be an object")
    limits = raw.get("limits", {})
    if not isinstance(limits, dict):
        raise ConfigError("limits must be an object")
    cfg.max_streams = _integer(limits.get("max_streams", cfg.max_streams), "limits.max_streams",
                               minimum=1, maximum=4096)
    cfg.connect_timeout = _number(limits.get("connect_timeout", cfg.connect_timeout),
                                  "limits.connect_timeout", minimum=0, maximum=300)
    cfg.idle_timeout = _number(limits.get("idle_timeout", cfg.idle_timeout),
                               "limits.idle_timeout", minimum=0, allow_zero=True)
    return cfg


def parse_allowed_target(value: str) -> AllowedTarget:
    network_part, separator, port_part = value.rpartition(":")
    try:
        candidate = ipaddress.ip_network(network_part.strip("[]"), strict=False) if separator else None
    except ValueError:
        candidate = None
    if candidate is None:
        network_part, port_part = value, "1-65535"
        try:
            network = ipaddress.ip_network(network_part.strip("[]"), strict=False)
        except ValueError as exc:
            raise ConfigError(f"invalid network in allow-target: {value}") from exc
    else:
        network = candidate
    try:
        if port_part == "*":
            first, last = 1, 65535
        elif "-" in port_part:
            a, b = port_part.split("-", 1)
            first, last = int(a), int(b)
        else:
            first = last = int(port_part)
    except ValueError as exc:
        raise ConfigError(f"invalid port range in allow-target: {value}") from exc
    if not 1 <= first <= last <= 65535:
        raise ConfigError(f"invalid port range in allow-target: {value}")
    return AllowedTarget(network, first, last)


def load_agent_config(path: str | Path | None) -> AgentConfig:
    raw = _load_json(path)
    cfg = AgentConfig()
    if "allow_targets" in raw:
        if not isinstance(raw["allow_targets"], list):
            raise ConfigError("allow_targets must be an array")
        cfg.allow_targets = [parse_allowed_target(str(v)) for v in raw["allow_targets"]]
    cfg.enable_reverse = _boolean(raw, "enable_reverse", cfg.enable_reverse)
    cfg.allow_nonloopback_reverse = _boolean(raw, "allow_nonloopback_reverse", cfg.allow_nonloopback_reverse)
    cfg.enable_control = _boolean(raw, "enable_control", cfg.enable_control)
    cfg.enable_exec = _boolean(raw, "enable_exec", cfg.enable_exec)
    cfg.enable_file_transfer = _boolean(raw, "enable_file_transfer", cfg.enable_file_transfer)
    file_root = raw.get("file_root", cfg.file_root)
    if not isinstance(file_root, str):
        raise ConfigError("file_root must be a string")
    cfg.file_root = file_root
    cfg.max_streams = _integer(raw.get("max_streams", cfg.max_streams), "max_streams",
                               minimum=1, maximum=4096)
    cfg.connect_timeout = _number(raw.get("connect_timeout", cfg.connect_timeout),
                                  "connect_timeout", minimum=0, maximum=300)
    return cfg
