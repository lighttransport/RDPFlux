from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from .config import load_agent_config, parse_allowed_target
from .forwarding import AgentForwarder
from .mux import MuxPeer
from .paths import default_agent_config, optional_config
from .windows_wts import open_agent_transport


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rdpflux-agent")
    parser.add_argument("--transport", choices=("auto", "dvc", "svc"), default="auto")
    parser.add_argument("--config")
    parser.add_argument("--allow-target", action="append", default=[])
    parser.add_argument("--enable-reverse", action="store_true")
    parser.add_argument("--allow-nonloopback-reverse", action="store_true")
    parser.add_argument("--retry", type=float, default=2.0, help="seconds between channel-open attempts")
    parser.add_argument("--once", action="store_true", help="exit instead of waiting for an RDP reconnect")
    parser.add_argument("--verbose", action="store_true")
    return parser


async def _run(args) -> None:
    config = load_agent_config(optional_config(args.config, default_agent_config()))
    config.allow_targets.extend(parse_allowed_target(value) for value in args.allow_target)
    config.enable_reverse = config.enable_reverse or args.enable_reverse
    config.allow_nonloopback_reverse = config.allow_nonloopback_reverse or args.allow_nonloopback_reverse
    while True:
        try:
            transport = await asyncio.to_thread(open_agent_transport, args.transport)
            logging.info("attached to RDP channel %s", transport.channel_name)
            peer = MuxPeer(transport, role="agent", max_streams=config.max_streams)
            forwarder = AgentForwarder(peer, config)
            try:
                await forwarder.start()
                await peer.wait_closed()
            finally:
                await forwarder.close()
        except Exception as exc:
            logging.warning("RDP channel unavailable: %s", exc)
        if args.once:
            return
        await asyncio.sleep(max(0.1, args.retry))


def main(argv: list[str] | None = None) -> int:
    if os.name != "nt":
        print("rdpflux-agent must run inside a Windows RDP session", file=sys.stderr)
        return 2
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
