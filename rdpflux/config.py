from __future__ import annotations

import ipaddress
import json
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
    if not host:
        if default_host is None:
            raise ConfigError(f"endpoint has an empty host: {value}")
        host = default_host
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
    return ForwardRule(parse_endpoint(value["listen"]), parse_endpoint(value["target"]), str(value.get("name", "")))


def load_client_config(path: str | Path | None) -> ClientConfig:
    raw = _load_json(path)
    cfg = ClientConfig()
    cfg.local_forwards = [_rule(v, "local_forwards") for v in raw.get("local_forwards", [])]
    cfg.reverse_forwards = [_rule(v, "reverse_forwards") for v in raw.get("reverse_forwards", [])]
    cfg.socks = [parse_endpoint(v if isinstance(v, str) else v.get("listen", "")) for v in raw.get("socks", [])]
    control = raw.get("control")
    if isinstance(control, dict):
        listen = control.get("listen")
        if isinstance(listen, str) and listen:
            cfg.control_listen = parse_endpoint(listen, default_host="127.0.0.1")
        cfg.control_token = str(control.get("token", ""))
    limits = raw.get("limits", {})
    if not isinstance(limits, dict):
        raise ConfigError("limits must be an object")
    cfg.max_streams = int(limits.get("max_streams", cfg.max_streams))
    cfg.connect_timeout = float(limits.get("connect_timeout", cfg.connect_timeout))
    cfg.idle_timeout = float(limits.get("idle_timeout", cfg.idle_timeout))
    if not 1 <= cfg.max_streams <= 4096:
        raise ConfigError("max_streams must be between 1 and 4096")
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
    if port_part == "*":
        first, last = 1, 65535
    elif "-" in port_part:
        a, b = port_part.split("-", 1)
        first, last = int(a), int(b)
    else:
        first = last = int(port_part)
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
    cfg.enable_reverse = bool(raw.get("enable_reverse", cfg.enable_reverse))
    cfg.allow_nonloopback_reverse = bool(raw.get("allow_nonloopback_reverse", cfg.allow_nonloopback_reverse))
    cfg.enable_control = bool(raw.get("enable_control", cfg.enable_control))
    cfg.enable_exec = bool(raw.get("enable_exec", cfg.enable_exec))
    cfg.enable_file_transfer = bool(raw.get("enable_file_transfer", cfg.enable_file_transfer))
    cfg.file_root = str(raw.get("file_root", cfg.file_root))
    cfg.max_streams = int(raw.get("max_streams", cfg.max_streams))
    cfg.connect_timeout = float(raw.get("connect_timeout", cfg.connect_timeout))
    return cfg
