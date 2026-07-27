from __future__ import annotations

import os
from pathlib import Path


def default_client_config() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "rdpflux" / "client.json"


def default_client_log() -> Path:
    """Where the mstsc COM server logs, since its stderr is not attached to a console."""
    return default_client_config().parent / "client.log"


def default_agent_config() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return root / "rdpflux" / "agent.json"


def optional_config(path: str | None, default: Path) -> Path | None:
    if path:
        return Path(path)
    return default if default.exists() else None
