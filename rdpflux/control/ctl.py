"""rdpflux-ctl: command-line client for the control REST API.

Runs short commands and transfers files through a running RDPFlux client's
control listener. The listener URL and bearer token come from --url/--token or
the RDPFLUX_URL / RDPFLUX_TOKEN environment variables.

  rdpflux-ctl exec -- powershell -NoProfile -Command "Get-Date"
  rdpflux-ctl put local.bin work:/incoming/local.bin --create-parents
  rdpflux-ctl get work:/logs/run.log run.log
  rdpflux-ctl ls work:/logs
  rdpflux-ctl diag

Transport limits (see README "Message size limits"): a control request or
reply carries a JSON header of at most 64 KiB, so keep exec commands short and
move large data with put/get (/v1/file streams it as a raw body). Exec output
that does not fit is truncated by the agent; redirect it to a file and fetch
it with get.

Retries: transient failures (connection errors, HTTP 502) are retried for
get, ls, diag and put, which are safe to repeat. exec is not retried unless
--retries is given, because a 502 can arrive after the command already ran.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .client import ControlError
from .http_client import HTTPControlClient

DEFAULT_URL = "http://127.0.0.1:18080"


def _transient(exc: Exception) -> bool:
    if isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError)):
        return True
    return isinstance(exc, ControlError) and str(exc).startswith("HTTP 502")


async def _with_retries(action: Callable[[], Awaitable[Any]], retries: int, delay: float) -> Any:
    for attempt in range(retries + 1):
        try:
            return await action()
        except Exception as exc:  # noqa: BLE001 - classified below
            if attempt == retries or not _transient(exc):
                raise
            print(f"rdpflux-ctl: transient failure ({exc}); retry {attempt + 1}/{retries}",
                  file=sys.stderr)
            await asyncio.sleep(delay * (attempt + 1))
    raise AssertionError("unreachable")


async def _exec(client: HTTPControlClient, args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("rdpflux-ctl exec: a command is required")
    params: dict[str, Any] = {"command": command}
    if args.timeout is not None:
        params["timeout"] = args.timeout
    if args.cwd:
        params["cwd"] = args.cwd
    result, _ = await _with_retries(lambda: client.request("exec", params), args.retries, args.retry_delay)
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        sys.stdout.write(result.get("stdout", ""))
        if result.get("stderr"):
            sys.stderr.write(result["stderr"])
        if result.get("truncated"):
            print("rdpflux-ctl: output truncated: " +
                  result.get("truncated_reason", "it exceeded the reply limit"), file=sys.stderr)
    code = result.get("exit_code")
    return code if isinstance(code, int) and 0 <= code < 256 else (1 if code else 0)


async def _put(client: HTTPControlClient, args: argparse.Namespace) -> int:
    data = Path(args.local).read_bytes()
    result = await _with_retries(
        lambda: client.write_file(args.remote, data, create_parents=args.create_parents),
        args.retries, args.retry_delay)
    size = result.get("size")
    if size is not None and int(size) != len(data):
        print(f"rdpflux-ctl: size mismatch after upload: wrote {len(data)} bytes, remote reports {size}",
              file=sys.stderr)
        return 1
    print(f"{args.local} -> {result.get('path', args.remote)} ({len(data)} bytes)")
    return 0


async def _get(client: HTTPControlClient, args: argparse.Namespace) -> int:
    _, data = await _with_retries(lambda: client.read_file(args.remote), args.retries, args.retry_delay)
    target = Path(args.local)
    temporary = target.with_name(target.name + ".part")
    temporary.write_bytes(data)
    temporary.replace(target)
    print(f"{args.remote} -> {target} ({len(data)} bytes)")
    return 0


async def _ls(client: HTTPControlClient, args: argparse.Namespace) -> int:
    result = await _with_retries(lambda: client.list_dir(args.path), args.retries, args.retry_delay)
    if args.json:
        print(json.dumps(result, indent=1))
        return 0
    for entry in result.get("entries", []):
        kind = "d" if entry.get("dir") else "-"
        print(f"{kind} {entry.get('size', 0):>12} {entry.get('name', '')}")
    if result.get("truncated"):
        print("rdpflux-ctl: listing truncated", file=sys.stderr)
    return 0


async def _diag(client: HTTPControlClient, args: argparse.Namespace) -> int:
    result, _ = await _with_retries(lambda: client.request("system_diagnostics"), args.retries, args.retry_delay)
    print(json.dumps(result, indent=1))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rdpflux-ctl", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("RDPFLUX_URL", DEFAULT_URL),
                        help="control listener URL (default: $RDPFLUX_URL or %(default)s)")
    parser.add_argument("--token", default=os.environ.get("RDPFLUX_TOKEN", ""),
                        help="bearer token (default: $RDPFLUX_TOKEN)")
    parser.add_argument("--http-timeout", type=float, default=330.0,
                        help="seconds to wait for one HTTP reply (default %(default)s)")
    parser.add_argument("--retry-delay", type=float, default=5.0)
    sub = parser.add_subparsers(dest="action", required=True)

    run = sub.add_parser("exec", help="run a short command (argv, no shell)")
    run.add_argument("--timeout", type=float, help="remote command timeout in seconds (max 300)")
    run.add_argument("--cwd")
    run.add_argument("--json", action="store_true", help="print the full JSON result")
    run.add_argument("--retries", type=int, default=0,
                     help="retry transient failures (default 0: the command may already have run)")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(handler=_exec)

    put = sub.add_parser("put", help="upload a local file (any size, streamed)")
    put.add_argument("local")
    put.add_argument("remote", help="root-relative remote path, e.g. work:/dir/file")
    put.add_argument("--create-parents", action="store_true")
    put.add_argument("--retries", type=int, default=3)
    put.set_defaults(handler=_put)

    get = sub.add_parser("get", help="download a remote file (any size, streamed)")
    get.add_argument("remote")
    get.add_argument("local")
    get.add_argument("--retries", type=int, default=3)
    get.set_defaults(handler=_get)

    ls = sub.add_parser("ls", help="list a remote directory")
    ls.add_argument("path", nargs="?", default=".")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--retries", type=int, default=3)
    ls.set_defaults(handler=_ls)

    diag = sub.add_parser("diag", help="remote host diagnostics")
    diag.add_argument("--retries", type=int, default=3)
    diag.set_defaults(handler=_diag)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = HTTPControlClient(args.url, args.token, timeout=args.http_timeout)
    started = time.monotonic()
    try:
        return asyncio.run(args.handler(client, args))
    except ControlError as exc:
        print(f"rdpflux-ctl: {exc}", file=sys.stderr)
        return 2
    except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
        print(f"rdpflux-ctl: cannot reach {args.url}: {exc or type(exc).__name__}"
              f" after {time.monotonic() - started:.1f} s", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
