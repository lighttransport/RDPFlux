from __future__ import annotations

import fnmatch
from pathlib import PureWindowsPath
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from .actions import ActionError

DEFAULT_MAX_UPLOAD = 128 * 1024 * 1024
MAX_ENTRIES = 5000
FILE_MODES = frozenset(("read", "write", "read_write"))


@dataclass(frozen=True, slots=True)
class FileRule:
    pattern: str
    mode: str

    def __post_init__(self) -> None:
        if not isinstance(self.pattern, str) or not self.pattern.strip():
            raise ValueError("file rule pattern must be a non-empty string")
        if self.mode not in FILE_MODES:
            raise ValueError(f"unsupported file rule mode: {self.mode}")


@dataclass(frozen=True, slots=True)
class FileRoot:
    name: str
    path: Path
    allowlist: list[FileRule] | None = None
    denylist: list[str] | None = None


class FileStore:
    """Read and write files beneath one directory with glob-based permissions."""

    def __init__(self, root: str | Path | None = None, *, allowlist: list[FileRule] | None = None,
                 denylist: list[str] | None = None, roots: list[FileRoot] | None = None,
                 max_upload: int = DEFAULT_MAX_UPLOAD) -> None:
        if isinstance(max_upload, bool) or not isinstance(max_upload, int) or max_upload <= 0:
            raise ValueError("max_upload must be a positive integer")
        self.max_upload = max_upload
        if roots is not None:
            if not roots:
                raise ValueError("file roots must not be empty")
            normalized = []
            names = set()
            for item in roots:
                name = item.name.strip()
                if not name or name.casefold() in names or ":" in name or "/" in name or "\\" in name:
                    raise ValueError(f"invalid or duplicate file root name: {item.name!r}")
                path = Path(item.path).expanduser().resolve()
                if not path.is_dir():
                    raise ValueError(f"file root {path} is not a directory")
                names.add(name.casefold())
                normalized.append(FileRoot(name, path, item.allowlist,
                                           [self._pattern(value) for value in (item.denylist or [])]))
            self._roots = normalized
        else:
            if not root:
                raise ValueError("file transfer requires a root directory")
            path = Path(root).expanduser().resolve()
            if not path.is_dir():
                raise ValueError(f"file root {path} is not a directory")
            self._roots = [FileRoot("", path, allowlist,
                                    [self._pattern(value) for value in (denylist or [])])]
        self.root = self._roots[0].path
        self.allowlist = self._roots[0].allowlist
        self.denylist = self._roots[0].denylist or []

    @staticmethod
    def _pattern(value: str) -> str:
        return value.replace("\\", "/").strip("/").casefold()

    def _relative(self, root: FileRoot, target: Path) -> str:
        relative = target.relative_to(root.path).as_posix().strip("/")
        return relative.casefold()

    @staticmethod
    def _matches(pattern: str, relative: str) -> bool:
        if fnmatch.fnmatchcase(relative, pattern):
            return True
        # A directory covered by "folder/**" is itself in that subtree.
        return fnmatch.fnmatchcase(relative + "/__rdpflux_child__", pattern)

    def _can(self, root: FileRoot, target: Path, operation: str) -> bool:
        relative = self._relative(root, target)
        denylist = root.denylist or []
        if any(self._matches(pattern, relative) for pattern in denylist):
            return False
        if root.allowlist is None:
            return True
        return any(rule.mode in (operation, "read_write")
                   and self._matches(self._pattern(rule.pattern), relative)
                   for rule in root.allowlist)

    def _check_access(self, root: FileRoot, target: Path, operation: str, requested: str) -> None:
        if not self._can(root, target, operation):
            raise ActionError(f"{operation} access is denied by the file policy: {requested}")

    def _select_root(self, path: str) -> tuple[FileRoot, str]:
        if len(self._roots) == 1 and not self._roots[0].name:
            return self._roots[0], path
        if ":" not in path:
            raise ActionError("multiple file roots require a root prefix such as temp:/path")
        name, relative = path.split(":", 1)
        for root in self._roots:
            if root.name.casefold() == name.casefold():
                return root, relative.lstrip("/")
        raise ActionError(f"unknown file root: {name}")

    def _resolve(self, path: str) -> tuple[FileRoot, Path]:
        if not isinstance(path, str) or not path:
            raise ActionError("path must be a non-empty string")
        root, relative = self._select_root(path)
        windows_path = PureWindowsPath(relative)
        if Path(relative).is_absolute() or windows_path.drive or windows_path.root:
            raise ActionError(f"path escapes the file root: {path}")
        candidate = (root.path / relative).resolve()
        if candidate != root.path and root.path not in candidate.parents:
            raise ActionError(f"path escapes the file root: {path}")
        return root, candidate

    def resolve(self, path: str) -> Path:
        """Resolve a request path inside the root, rejecting anything that escapes.

        Resolution happens before the containment check so '..' segments, symlinks,
        and absolute paths are all normalised away rather than matched as text.
        """
        return self._resolve(path)[1]

    def read(self, path: str) -> tuple[dict[str, Any], bytes]:
        root, target = self._resolve(path)
        self._check_access(root, target, "read", path)
        if not target.is_file():
            raise ActionError(f"not a file: {path}")
        size = target.stat().st_size
        return {"path": str(target), "size": size}, target.read_bytes()

    def write(self, path: str, data: bytes, *, create_parents: bool = False) -> dict[str, Any]:
        root, target = self._resolve(path)
        self._check_access(root, target, "write", path)
        if len(data) > self.max_upload:
            raise ActionError(f"write of {len(data)} bytes exceeds the {self.max_upload} byte upload limit")
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
        root, target = self._resolve(path)
        self._check_access(root, target, "read", path)
        if not target.is_dir():
            raise ActionError(f"not a directory: {path}")
        entries = []
        for child in sorted(target.iterdir(), key=lambda item: item.name):
            if len(entries) >= MAX_ENTRIES:
                break
            try:
                child_target = child.resolve()
                visible = self._can(root, child_target, "read")
            except (OSError, RuntimeError, ValueError):
                visible = False
            if not visible:
                continue
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
