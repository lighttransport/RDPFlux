from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from .config import ClientConfig, ForwardRule, parse_endpoint, load_client_config
from .forwarding import ClientForwarder
from .mux import MuxPeer
from .paths import default_client_config, default_client_log, optional_config
from .registry import register, unregister
from .transport import FreeRDPStdioTransport

LOG = logging.getLogger(__name__)
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUP_COUNT = 3


def _forward(value: str) -> ForwardRule:
    if "=" not in value:
        raise argparse.ArgumentTypeError("forward must be LISTEN=TARGET")
    listen, target = value.split("=", 1)
    try:
        return ForwardRule(parse_endpoint(listen, default_host="127.0.0.1"), parse_endpoint(target))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rdpflux-client")
    sub = parser.add_subparsers(dest="command")
    for name in ("register", "unregister"):
        item = sub.add_parser(name)
        item.add_argument("--machine", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("--transport", choices=("mstsc", "freerdp"), default="mstsc" if os.name == "nt" else "freerdp")
    run.add_argument("--config")
    run.add_argument("--local", action="append", type=_forward, default=[])
    run.add_argument("--socks", action="append", default=[])
    run.add_argument("--reverse", action="append", type=_forward, default=[])
    run.add_argument("--verbose", action="store_true")
    run.add_argument("--log-file", help="write logs here; defaults to the mstsc plugin log when run by COM")
    run.add_argument("--no-log-file", action="store_true", help="disable the default mstsc plugin log file")
    run.add_argument("--com-smoke-test", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--control-listen", help="expose the desktop-control REST API on this loopback endpoint")
    run.add_argument("--control-token", help="bearer token guarding the control API")
    mcp = sub.add_parser("mcp", help="serve MCP over stdio, bridging to a running control REST API")
    mcp.add_argument("--url", default="http://127.0.0.1:18080",
                     help="base URL of the client's control REST API")
    mcp.add_argument("--token", default="", help="bearer token for the control API")
    mcp.add_argument("--enable-exec", action="store_true", help="expose the run_command tool")
    mcp.add_argument("--enable-file-transfer", action="store_true", help="expose the file tools")
    return parser


def _log_path(args, embedding: bool) -> Path | None:
    if args.no_log_file:
        return None
    if args.log_file:
        return Path(args.log_file)
    # A COM-launched server has no console, so its stderr goes nowhere.
    return default_client_log() if embedding else None


def _configure_logging(args, embedding: bool) -> None:
    level = logging.DEBUG if args.verbose else logging.INFO
    handlers: list[logging.Handler] = []
    path = _log_path(args, embedding)
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.handlers.RotatingFileHandler(
                path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
            ))
        except OSError as exc:  # unwritable path; keep going on stderr
            print(f"cannot open log file {path}: {exc}", file=sys.stderr)
            path = None
    if not embedding or not handlers:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=level, format=LOG_FORMAT, handlers=handlers, force=True)
    if path is not None:
        LOG.info("logging to %s", path)


def _config(args) -> ClientConfig:
    cfg = load_client_config(optional_config(args.config, default_client_config()))
    cfg.local_forwards.extend(args.local)
    cfg.reverse_forwards.extend(args.reverse)
    for value in args.socks:
        cfg.socks.append(parse_endpoint(value, default_host="127.0.0.1"))
    if getattr(args, "control_listen", None):
        cfg.control_listen = parse_endpoint(args.control_listen, default_host="127.0.0.1")
    if getattr(args, "control_token", None):
        cfg.control_token = args.control_token
    return cfg


def _run_mcp(args) -> int:
    from .control.http_client import HTTPControlClient
    from .control.mcp import MCPServer

    server = MCPServer(
        HTTPControlClient(args.url, args.token),
        exec_enabled=args.enable_exec,
        files_enabled=args.enable_file_transfer,
    )
    try:
        asyncio.run(server.serve_stdio())
    except KeyboardInterrupt:
        pass
    return 0


async def _run_freerdp(config: ClientConfig) -> None:
    transport = FreeRDPStdioTransport()
    peer = MuxPeer(transport, role="client", max_streams=config.max_streams)
    forwarder = ClientForwarder(peer, config)
    try:
        await forwarder.start()
        await peer.wait_closed()
    finally:
        await forwarder.close()


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    embedding = any(value.lower() in ("-embedding", "/embedding") for value in raw)
    if embedding:
        raw = [value for value in raw if value.lower() not in ("-embedding", "/embedding")]
        raw = ["run", "--transport", "mstsc", *raw]
    args = _parser().parse_args(raw)
    if args.command == "register":
        register(machine=args.machine)
        print("mstsc plugin registered; restart mstsc before connecting")
        return 0
    if args.command == "unregister":
        unregister(machine=args.machine)
        print("mstsc plugin unregistered")
        return 0
    if args.command == "mcp":
        return _run_mcp(args)
    if args.command != "run":
        _parser().print_help()
        return 2
    _configure_logging(args, embedding)
    # A COM-launched server has nowhere to print a traceback, so anything that
    # escapes here would look like a silent startup failure in the log.
    try:
        config = _config(args)
        LOG.info("config: %d local, %d socks, %d reverse", len(config.local_forwards),
                 len(config.socks), len(config.reverse_forwards))
        if args.transport == "freerdp":
            asyncio.run(_run_freerdp(config))
            return 0
        if os.name != "nt":
            raise RuntimeError("mstsc transport requires Windows")
        from .mstsc_plugin import run_com_server
        return run_com_server(config, smoke_test=args.com_smoke_test,
                              config_factory=lambda: _config(args))
    except KeyboardInterrupt:
        LOG.info("interrupted; exiting")
        return 130
    except Exception:
        LOG.exception("client failed to start")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
