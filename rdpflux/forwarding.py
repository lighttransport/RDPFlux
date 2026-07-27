from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import struct
from dataclasses import dataclass
from typing import Any

from .config import AgentConfig, ClientConfig, Endpoint, ForwardRule
from .mux import MuxPeer, MuxStream
from .policy import resolve_allowed, validate_reverse_listener

LOG = logging.getLogger(__name__)


async def bridge_socket(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, stream: MuxStream,
                        idle_timeout: float = 0.0) -> None:
    loop = asyncio.get_running_loop()
    last_activity = loop.time()

    async def abort(reason: str) -> None:
        with contextlib.suppress(Exception):
            await stream.close(reason)
        writer.close()

    async def socket_to_mux() -> None:
        nonlocal last_activity
        try:
            while data := await reader.read(16 * 1024):
                last_activity = loop.time()
                await stream.write(data)
            await stream.write_eof()
        except (ConnectionError, OSError) as exc:
            await abort(str(exc))

    async def mux_to_socket() -> None:
        nonlocal last_activity
        try:
            while data := await stream.read(16 * 1024):
                last_activity = loop.time()
                writer.write(data)
                await writer.drain()
            if writer.can_write_eof():
                writer.write_eof()
                await writer.drain()
        except (ConnectionError, OSError) as exc:
            await abort(str(exc))

    async def idle_monitor() -> None:
        if idle_timeout <= 0:
            return
        while True:
            await asyncio.sleep(min(1.0, idle_timeout))
            if loop.time() - last_activity >= idle_timeout:
                await abort("idle timeout")
                return

    tasks = [asyncio.create_task(socket_to_mux()), asyncio.create_task(mux_to_socket())]
    monitor = asyncio.create_task(idle_monitor()) if idle_timeout > 0 else None
    try:
        await asyncio.gather(*tasks)
    finally:
        if monitor:
            monitor.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if monitor:
            await asyncio.gather(monitor, return_exceptions=True)
        with contextlib.suppress(Exception):
            await stream.close()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


class ClientForwarder:
    def __init__(self, peer: MuxPeer, config: ClientConfig) -> None:
        self.peer = peer
        self.config = config
        self.servers: list[asyncio.AbstractServer] = []
        self.tasks: set[asyncio.Task[Any]] = set()
        self.reverse_rules: dict[str, ForwardRule] = {}
        self.peer.set_handlers(on_open=self._on_open)

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def start(self) -> None:
        await self.peer.start()
        await self.peer.wait_ready()
        for rule in self.config.local_forwards:
            server = await asyncio.start_server(
                lambda r, w, item=rule: self._spawn(self._handle_local(r, w, item)),
                rule.listen.host, rule.listen.port,
            )
            self.servers.append(server)
            LOG.info("local forward %s -> %s", rule.listen, rule.target)
        for endpoint in self.config.socks:
            server = await asyncio.start_server(
                lambda r, w: self._spawn(self._handle_socks(r, w)), endpoint.host, endpoint.port,
            )
            self.servers.append(server)
            LOG.info("SOCKS5 listening on %s", endpoint)
        for index, rule in enumerate(self.config.reverse_forwards):
            rule_id = rule.name or f"reverse-{index + 1}"
            if rule_id in self.reverse_rules:
                raise ValueError(f"duplicate reverse rule name {rule_id}")
            self.reverse_rules[rule_id] = rule
            await self.peer.request_listener({"kind": "reverse", "rule_id": rule_id, "listen": str(rule.listen)})
            LOG.info("reverse forward %s -> client %s", rule.listen, rule.target)

    async def _handle_local(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, rule: ForwardRule) -> None:
        try:
            stream = await self.peer.open_stream({"kind": "tcp", "host": rule.target.host, "port": rule.target.port}, self.config.connect_timeout)
            await bridge_socket(reader, writer, stream, self.config.idle_timeout)
        except Exception as exc:
            LOG.warning("forward to %s failed: %s", rule.target, exc)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _handle_socks(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        stream: MuxStream | None = None
        try:
            version, methods_count = await reader.readexactly(2)
            methods = await reader.readexactly(methods_count)
            if version != 5 or 0 not in methods:
                writer.write(b"\x05\xff")
                await writer.drain()
                return
            writer.write(b"\x05\x00")
            await writer.drain()
            version, command, reserved, address_type = await reader.readexactly(4)
            if version != 5 or command != 1 or reserved != 0:
                await self._socks_reply(writer, 7)
                return
            if address_type == 1:
                host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
            elif address_type == 4:
                host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
            elif address_type == 3:
                length = (await reader.readexactly(1))[0]
                if length == 0:
                    raise ValueError("empty SOCKS hostname")
                host = (await reader.readexactly(length)).decode("idna")
            else:
                await self._socks_reply(writer, 8)
                return
            (port,) = struct.unpack("!H", await reader.readexactly(2))
            stream = await self.peer.open_stream({"kind": "tcp", "host": host, "port": port}, self.config.connect_timeout)
            await self._socks_reply(writer, 0)
            await bridge_socket(reader, writer, stream, self.config.idle_timeout)
        except (asyncio.IncompleteReadError, UnicodeError):
            pass
        except Exception as exc:
            LOG.warning("SOCKS connection failed: %s", exc)
            if stream is None:
                with contextlib.suppress(Exception):
                    await self._socks_reply(writer, 5)
        finally:
            if not writer.is_closing():
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

    @staticmethod
    async def _socks_reply(writer: asyncio.StreamWriter, status: int) -> None:
        writer.write(bytes((5, status, 0, 1)) + b"\x00\x00\x00\x00\x00\x00")
        await writer.drain()

    async def _on_open(self, stream: MuxStream, metadata: dict[str, Any]) -> None:
        if metadata.get("kind") != "reverse":
            raise ValueError("agent may only initiate configured reverse streams")
        rule_id = metadata.get("rule_id")
        if not isinstance(rule_id, str) or rule_id not in self.reverse_rules:
            raise ValueError("unknown reverse rule")
        target = self.reverse_rules[rule_id].target
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target.host, target.port), self.config.connect_timeout,
        )
        self._spawn(bridge_socket(reader, writer, stream, self.config.idle_timeout))

    async def close(self) -> None:
        for server in self.servers:
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in self.servers), return_exceptions=True)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.peer.close()


@dataclass(slots=True)
class _ReverseListener:
    rule_id: str
    server: asyncio.AbstractServer


class AgentForwarder:
    def __init__(self, peer: MuxPeer, config: AgentConfig) -> None:
        self.peer = peer
        self.config = config
        self.listeners: dict[str, _ReverseListener] = {}
        self.tasks: set[asyncio.Task[Any]] = set()
        self.peer.set_handlers(on_open=self._on_open, on_listen=self._on_listen)

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def start(self) -> None:
        await self.peer.start()
        await self.peer.wait_ready()

    async def _connect_allowed(self, target: Endpoint) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        addresses = await resolve_allowed(target, self.config)
        last_error: Exception | None = None
        for family, address in addresses:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(address, target.port, family=family), self.config.connect_timeout,
                )
            except (OSError, asyncio.TimeoutError) as exc:
                last_error = exc
        raise ConnectionError(f"cannot connect to {target}: {last_error}")

    async def _on_open(self, stream: MuxStream, metadata: dict[str, Any]) -> None:
        if metadata.get("kind") != "tcp":
            raise ValueError("unsupported stream kind")
        host, port = metadata.get("host"), metadata.get("port")
        if not isinstance(host, str) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("invalid TCP destination")
        reader, writer = await self._connect_allowed(Endpoint(host, port))
        self._spawn(bridge_socket(reader, writer, stream))

    async def _on_listen(self, request_id: int, metadata: dict[str, Any]) -> dict[str, Any]:
        if metadata.get("kind") != "reverse":
            raise ValueError("unsupported listener kind")
        rule_id, raw_listen = metadata.get("rule_id"), metadata.get("listen")
        if not isinstance(rule_id, str) or not rule_id or not isinstance(raw_listen, str):
            raise ValueError("invalid reverse listener")
        if rule_id in self.listeners:
            raise ValueError("reverse listener already exists")
        from .config import parse_endpoint
        endpoint = parse_endpoint(raw_listen)
        validate_reverse_listener(endpoint, self.config)
        server = await asyncio.start_server(
            lambda r, w: self._spawn(self._handle_reverse(r, w, rule_id)), endpoint.host, endpoint.port,
        )
        self.listeners[rule_id] = _ReverseListener(rule_id, server)
        return {"rule_id": rule_id, "listen": str(endpoint)}

    async def _handle_reverse(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, rule_id: str) -> None:
        try:
            stream = await self.peer.open_stream({"kind": "reverse", "rule_id": rule_id}, self.config.connect_timeout)
            await bridge_socket(reader, writer, stream)
        except Exception as exc:
            LOG.warning("reverse stream %s failed: %s", rule_id, exc)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def close(self) -> None:
        for listener in self.listeners.values():
            listener.server.close()
        await asyncio.gather(*(x.server.wait_closed() for x in self.listeners.values()), return_exceptions=True)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.peer.close()
