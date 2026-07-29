from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import sys

from .config import load_agent_config, parse_allowed_target
from .forwarding import AgentForwarder
from .mux import MuxPeer, describe_exception
from .paths import default_agent_config, optional_config
from .windows_wts import open_agent_transport


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rdpflux-agent")
    parser.add_argument("--transport", choices=("auto", "dvc", "svc"), default="auto")
    parser.add_argument("--config")
    parser.add_argument("--allow-target", action="append", default=[])
    parser.add_argument("--enable-reverse", action="store_true")
    parser.add_argument("--allow-nonloopback-reverse", action="store_true")
    parser.add_argument("--enable-control", action="store_true",
                        help="allow the client to capture the screen and inject input")
    parser.add_argument("--enable-exec", action="store_true",
                        help="allow the client to run commands (implies remote code execution)")
    parser.add_argument("--enable-file-transfer", action="store_true",
                        help="allow the client to read and write files under --file-root")
    parser.add_argument("--file-root", help="directory that file transfer is confined to")
    parser.add_argument("--retry", type=float, default=2.0, help="seconds between channel-open attempts")
    parser.add_argument("--once", action="store_true", help="exit instead of waiting for an RDP reconnect")
    parser.add_argument("--verbose", action="store_true")
    return parser


def _control_service(config):
    """Build the control service once; it outlives individual RDP channels."""
    if not config.enable_control:
        return None
    from .control.backend import WindowsBackend
    from .control.files import FileStore
    from .control.service import ControlService

    files = None
    if config.enable_file_transfer:
        if not config.file_root:
            raise RuntimeError("file transfer requires --file-root")
        files = FileStore(config.file_root)
    logging.info("desktop control enabled (exec=%s, file transfer=%s)",
                 config.enable_exec, config.enable_file_transfer)
    return ControlService(WindowsBackend(), allow_exec=config.enable_exec, files=files)


async def _run(args) -> None:
    if not math.isfinite(args.retry) or args.retry <= 0:
        raise ValueError("--retry must be a finite positive number")
    config = load_agent_config(optional_config(args.config, default_agent_config()))
    config.allow_targets.extend(parse_allowed_target(value) for value in args.allow_target)
    config.enable_reverse = config.enable_reverse or args.enable_reverse
    config.allow_nonloopback_reverse = config.allow_nonloopback_reverse or args.allow_nonloopback_reverse
    config.enable_control = config.enable_control or args.enable_control
    config.enable_exec = config.enable_exec or args.enable_exec
    config.enable_file_transfer = config.enable_file_transfer or args.enable_file_transfer
    config.file_root = args.file_root or config.file_root
    control = _control_service(config)
    while True:
        try:
            transport = await asyncio.to_thread(open_agent_transport, args.transport)
            logging.info("attached to RDP channel %s", transport.channel_name)
            peer = MuxPeer(transport, role="agent", max_streams=config.max_streams)
            forwarder = AgentForwarder(peer, config, control)
            try:
                await forwarder.start()
                logging.info("tunnel ready on %s; waiting for streams", transport.channel_name)
                await peer.wait_closed()
                if peer.close_error is None:
                    logging.info("RDP channel closed by the peer")
                else:
                    logging.warning("RDP channel failed: %s", describe_exception(peer.close_error))
            finally:
                await forwarder.close()
        except Exception as exc:
            logging.warning("RDP channel unavailable: %s", describe_exception(exc))
        if args.once:
            logging.info("exiting after one session (--once)")
            return
        delay = max(0.1, args.retry)
        logging.info("reconnecting in %.1fs", delay)
        await asyncio.sleep(delay)


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
        logging.info("interrupted; exiting")
        return 130
    except Exception:
        logging.exception("agent stopped by an unhandled error")
        return 1
    logging.info("agent loop finished; exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
