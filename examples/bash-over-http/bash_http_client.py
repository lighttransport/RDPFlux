#!/usr/bin/env python3
"""Run commands and transfer files through the RDPFlux HTTP control API.

This example is a Windows-first port of the bash-over-HTTP client from
``gemm/tools``. It uses only the standard library and the existing RDPFlux
captured-output API.

Examples::

    python bash_http_client.py health
    python bash_http_client.py exec --command "Get-Location"
    python bash_http_client.py --program bash exec --command "pwd"
    python bash_http_client.py ls .
    python bash_http_client.py shell

The legacy ``--command`` form remains supported.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


MAX_FILE = 64 * 1024 * 1024


class BashHTTPError(RuntimeError):
    """An HTTP or remote-command error."""


@dataclass(frozen=True)
class Result:
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.truncated


def _envelope(value: dict) -> dict:
    """Unwrap the control service's ``{ok, result}`` response envelope."""
    if value.get("ok") is False:
        raise BashHTTPError(str(value.get("error", "remote operation failed")))
    result = value.get("result", value)
    if not isinstance(result, dict):
        raise BashHTTPError("RDPFlux returned an invalid result")
    return result


class Client:
    """Synchronous client for the RDPFlux control REST API."""

    def __init__(self, base_url: str = "http://127.0.0.1:18080", token: str = "",
                 timeout: float = 30.0, program: str = "powershell") -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("url must include http:// or https:// and a host")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.program = program

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise BashHTTPError(f"HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise BashHTTPError(f"cannot reach {self.base_url}: {exc.reason}") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BashHTTPError("RDPFlux returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise BashHTTPError("RDPFlux returned a non-object response")
        return value

    def _request_bytes(self, method: str, path: str, body: bytes = b"") -> bytes:
        headers = {"Accept": "application/octet-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body:
            headers["Content-Type"] = "application/octet-stream"
        request = Request(self.base_url + path, data=body or None,
                          headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise BashHTTPError(f"HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise BashHTTPError(f"cannot reach {self.base_url}: {exc.reason}") from exc

    def health(self) -> dict:
        return self._request("GET", "/")

    def exec_argv(self, argv: list[str], *, cwd: str | None = None,
                  timeout: float | None = None) -> Result:
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise ValueError("argv must contain at least one non-empty string")
        payload: dict[str, object] = {"command": argv, "timeout": timeout or self.timeout}
        if cwd is not None:
            payload["cwd"] = cwd
        result = _envelope(self._request("POST", "/v1/exec", payload))
        return Result(result.get("exit_code"), str(result.get("stdout", "")),
                      str(result.get("stderr", "")), bool(result.get("truncated")))

    def run(self, command: str, *, cwd: str | None = None,
            timeout: float | None = None) -> Result:
        if not command.strip():
            raise ValueError("command must not be empty")
        if self.program.lower() in ("bash", "sh"):
            argv = [self.program, "-lc", command]
        elif Path(self.program).name.lower() in ("pwsh", "pwsh.exe", "powershell", "powershell.exe"):
            argv = [self.program, "-NoProfile", "-NonInteractive", "-Command", command]
        else:
            raise ValueError("unsupported shell; use bash, sh, powershell, or pwsh")
        return self.exec_argv(argv, cwd=cwd, timeout=timeout)

    def list_dir(self, path: str = ".") -> dict:
        return _envelope(self._request("GET", f"/v1/dir?path={quote(path, safe='')}"))

    def read_file(self, path: str) -> bytes:
        return self._request_bytes("GET", f"/v1/file?path={quote(path, safe='')}")

    def write_file(self, path: str, data: bytes, *, create_parents: bool = False) -> dict:
        if len(data) > MAX_FILE:
            raise ValueError(f"file exceeds the {MAX_FILE} byte limit")
        query = f"/v1/file?path={quote(path, safe='')}"
        if create_parents:
            query += "&create_parents=1"
        return _envelope(self._request_bytes_json("PUT", query, data))

    def _request_bytes_json(self, method: str, path: str, body: bytes) -> dict:
        headers = {"Accept": "application/json", "Content-Type": "application/octet-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise BashHTTPError(f"HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise BashHTTPError(f"cannot reach {self.base_url}: {exc.reason}") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BashHTTPError("RDPFlux returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise BashHTTPError("RDPFlux returned a non-object response")
        return value


class Shell:
    """Interactive wrapper with explicit client-side working-directory state."""

    def __init__(self, client: Client, cwd: str | None = None) -> None:
        self.client = client
        self.cwd = cwd

    def run(self, command: str) -> Result:
        return self.client.run(command, cwd=self.cwd)

    def set_cwd(self, cwd: str) -> None:
        if not cwd:
            raise ValueError("cwd must not be empty")
        self.cwd = cwd


def _print_result(result: Result, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"exit_code": result.exit_code, "stdout": result.stdout,
                          "stderr": result.stderr, "truncated": result.truncated}))
    else:
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
        if result.truncated:
            print("warning: remote output was truncated", file=sys.stderr)
    return 0 if result.ok else (result.exit_code if isinstance(result.exit_code, int) else 1)


def _print_listing(value: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(value))
        return 0
    for entry in value.get("entries", []):
        marker = "d" if entry.get("dir") else "-"
        size = "" if entry.get("dir") else str(entry.get("size", "?"))
        print(f"{marker} {size:>12} {entry.get('name', '')}")
    if value.get("truncated"):
        print("warning: directory listing was truncated", file=sys.stderr)
    return 0


def _read_local(path: str) -> bytes:
    target = Path(path)
    if not target.is_file():
        raise ValueError(f"local file does not exist: {path}")
    if target.stat().st_size > MAX_FILE:
        raise ValueError(f"local file exceeds the {MAX_FILE} byte limit")
    return target.read_bytes()


def _download(client: Client, remote: str, local: str, force: bool) -> int:
    target = Path(local)
    if target.exists() and not force:
        raise ValueError(f"local file exists; use --force to overwrite: {local}")
    data = client.read_file(remote)
    target.write_bytes(data)
    print(f"downloaded {len(data)} bytes to {target}")
    return 0


def _interactive(shell: Shell, client: Client, as_json: bool) -> int:
    print("Type :help for local commands; other input runs remotely.")
    while True:
        try:
            line = input("bash-http> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line.strip():
            continue
        try:
            if line.startswith(":"):
                parts = shlex.split(line[1:])
                if not parts:
                    continue
                op, rest = parts[0], parts[1:]
                if op in ("quit", "q", "exit"):
                    return 0
                if op == "help":
                    print(":help, :health, :ls [path], :cat PATH, :upload LOCAL REMOTE, "
                          ":download REMOTE LOCAL, :cwd PATH, :quit")
                elif op == "health":
                    print(json.dumps(client.health(), indent=2))
                elif op == "ls":
                    _print_listing(client.list_dir(rest[0] if rest else "."), as_json)
                elif op == "cat" and len(rest) == 1:
                    print(client.read_file(rest[0]).decode("utf-8", "replace"), end="")
                elif op == "upload" and len(rest) == 2:
                    client.write_file(rest[1], _read_local(rest[0]))
                    print("uploaded")
                elif op == "download" and len(rest) == 2:
                    _download(client, rest[0], rest[1], False)
                elif op == "cwd" and len(rest) == 1:
                    shell.set_cwd(rest[0])
                    print(f"cwd: {shell.cwd}")
                else:
                    raise ValueError("invalid built-in; use :help")
            else:
                code = _print_result(shell.run(line), as_json)
                if code:
                    print(f"[exit {code}]", file=sys.stderr)
        except (BashHTTPError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("RDPFLUX_CONTROL_URL", "http://127.0.0.1:18080"))
    parser.add_argument("--token", default=os.environ.get("RDPFLUX_CONTROL_TOKEN", ""))
    parser.add_argument("--program", default=os.environ.get("RDPFLUX_SHELL", "powershell"),
                        help="remote shell: powershell, pwsh, bash, or sh")
    parser.add_argument("--cwd", help="initial remote working directory")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    parser.add_argument("--command", dest="legacy_command", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="subcommand")
    sub.add_parser("health", help="check the control endpoint")
    sub.add_parser("shell", help="open an interactive command prompt")
    execute = sub.add_parser("exec", help="run a remote command")
    modes = execute.add_mutually_exclusive_group(required=True)
    modes.add_argument("--command", help="run text through the selected shell")
    modes.add_argument("--argv", nargs=argparse.REMAINDER, help="run argv directly")
    listing = sub.add_parser("ls", help="list a directory in file_root")
    listing.add_argument("path", nargs="?", default=".")
    cat = sub.add_parser("cat", help="read a UTF-8 file in file_root")
    cat.add_argument("path")
    upload = sub.add_parser("upload", help="upload a local file into file_root")
    upload.add_argument("local")
    upload.add_argument("remote")
    upload.add_argument("--create-parents", action="store_true")
    download = sub.add_parser("download", help="download a file from file_root")
    download.add_argument("remote")
    download.add_argument("local")
    download.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        client = Client(args.url, args.token, args.timeout, args.program)
        shell = Shell(client, args.cwd)
        if args.legacy_command is not None:
            return _print_result(shell.run(args.legacy_command), args.json)
        if args.subcommand in (None, "shell"):
            return _interactive(shell, client, args.json)
        if args.subcommand == "health":
            print(json.dumps(client.health(), indent=None if args.json else 2))
            return 0
        if args.subcommand == "exec":
            if args.command is not None:
                return _print_result(shell.run(args.command), args.json)
            argv = list(args.argv or [])
            if argv and argv[0] == "--":
                argv.pop(0)
            return _print_result(client.exec_argv(argv, cwd=shell.cwd, timeout=args.timeout), args.json)
        if args.subcommand == "ls":
            return _print_listing(client.list_dir(args.path), args.json)
        if args.subcommand == "cat":
            data = client.read_file(args.path)
            if args.json:
                print(json.dumps({"path": args.path, "text": data.decode("utf-8", "replace")}))
            else:
                sys.stdout.buffer.write(data)
            return 0
        if args.subcommand == "upload":
            result = client.write_file(args.remote, _read_local(args.local),
                                       create_parents=args.create_parents)
            print(json.dumps(result) if args.json else f"uploaded {args.local} -> {args.remote}")
            return 0
        if args.subcommand == "download":
            return _download(client, args.remote, args.local, args.force)
        raise ValueError("unknown command")
    except (BashHTTPError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
