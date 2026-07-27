from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from .config import ClientConfig, ForwardRule, parse_endpoint, load_client_config
from .forwarding import ClientForwarder
from .mux import MuxPeer
from .paths import default_client_config, optional_config
from .registry import register, unregister
from .transport import FreeRDPStdioTransport


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
    run.add_argument("--com-smoke-test", action="store_true", help=argparse.SUPPRESS)
    return parser


def _config(args) -> ClientConfig:
    cfg = load_client_config(optional_config(args.config, default_client_config()))
    cfg.local_forwards.extend(args.local)
    cfg.reverse_forwards.extend(args.reverse)
    for value in args.socks:
        cfg.socks.append(parse_endpoint(value, default_host="127.0.0.1"))
    return cfg


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
    if args.command != "run":
        _parser().print_help()
        return 2
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    config = _config(args)
    if args.transport == "freerdp":
        asyncio.run(_run_freerdp(config))
        return 0
    if os.name != "nt":
        raise RuntimeError("mstsc transport requires Windows")
    from .mstsc_plugin import run_com_server
    return run_com_server(config, smoke_test=args.com_smoke_test)


if __name__ == "__main__":
    raise SystemExit(main())
