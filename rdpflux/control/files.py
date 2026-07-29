from __future__ import annotations

from pathlib import Path
from typing import Any

from .actions import ActionError

MAX_FILE = 64 * 1024 * 1024
MAX_ENTRIES = 5000


class FileStore:
    """Read and write files beneath one directory, and nowhere else."""

    def __init__(self, root: str | Path) -> None:
        if not root:
            raise ValueError("file transfer requires a root directory")
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"file root {self.root} is not a directory")

    def resolve(self, path: str) -> Path:
        """Resolve a request path inside the root, rejecting anything that escapes.

        Resolution happens before the containment check so '..' segments, symlinks,
        and absolute paths are all normalised away rather than matched as text.
        """
        if not isinstance(path, str) or not path:
            raise ActionError("path must be a non-empty string")
        candidate = (self.root / path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ActionError(f"path escapes the file root: {path}")
        return candidate

    def read(self, path: str) -> tuple[dict[str, Any], bytes]:
        target = self.resolve(path)
        if not target.is_file():
            raise ActionError(f"not a file: {path}")
        size = target.stat().st_size
        if size > MAX_FILE:
            raise ActionError(f"file of {size} bytes exceeds the {MAX_FILE} byte limit")
        return {"path": str(target), "size": size}, target.read_bytes()

    def write(self, path: str, data: bytes, *, create_parents: bool = False) -> dict[str, Any]:
        target = self.resolve(path)
        if len(data) > MAX_FILE:
            raise ActionError(f"write of {len(data)} bytes exceeds the {MAX_FILE} byte limit")
        if target.is_dir():
            raise ActionError(f"is a directory: {path}")
        if create_parents:
            # resolve() already proved the parent is inside the root.
            target.parent.mkdir(parents=True, exist_ok=True)
        elif not target.parent.is_dir():
            raise ActionError(f"parent directory does not exist: {path}")
        target.write_bytes(data)
        return {"path": str(target), "size": len(data)}

    def list(self, path: str = ".") -> dict[str, Any]:
        target = self.resolve(path)
        if not target.is_dir():
            raise ActionError(f"not a directory: {path}")
        entries = []
        for child in sorted(target.iterdir(), key=lambda item: item.name):
            if len(entries) >= MAX_ENTRIES:
                break
            try:
                is_dir = child.is_dir()
                entries.append({
                    "name": child.name,
                    "dir": is_dir,
                    "size": None if is_dir else child.stat().st_size,
                })
            except OSError:
                continue  # vanished or unreadable between iterdir and stat
        return {"path": str(target), "entries": entries,
                "truncated": len(entries) >= MAX_ENTRIES}
