"""TCP proxy in front of SpyServer instances that knows which app holds which connection.

SpyServer clients announce themselves in their first command (HELLO: protocol version + client
name), so the proxy can tell apps apart even when they run on the same host. An app that is
switched off is disconnected and refused until it is switched on again.
"""
from __future__ import annotations

import asyncio
import collections
import itertools
import json
import logging
import os
import re
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("spyswitch")

CMD_HELLO, CMD_SET = 0, 2
SET_ENABLED, SET_GAIN, SET_IQ_FREQ, SET_FFT_FREQ = 1, 2, 101, 201
_SERVER_RE = re.compile(r"\s*(?P<name>[^@;]+?)\s*@\s*(?P<port>\d+)\s*=\s*(?P<host>[^;]+):(?P<up>\d+)\s*")


@dataclass(frozen=True)
class ServerSpec:
    name: str
    listen_port: int
    upstream_host: str
    upstream_port: int

    @property
    def key(self) -> str:
        return str(self.listen_port)


def parse_servers(text: str) -> list[ServerSpec]:
    """'VHF/UHF@5555=127.0.0.1:15555; HF@5556=127.0.0.1:15556'"""
    specs = []
    for part in filter(None, (p.strip() for p in text.split(";"))):
        m = _SERVER_RE.fullmatch(part)
        if not m:
            raise ValueError(f"Cannot read server '{part}'. Use NAME@LISTEN_PORT=UPSTREAM_HOST:PORT")
        specs.append(ServerSpec(m["name"], int(m["port"]), m["host"].strip("[]"), int(m["up"])))
    if len({s.listen_port for s in specs}) != len(specs):
        raise ValueError("Two servers use the same listening port")
    return specs


def clean_name(raw: bytes) -> str:
    name = "".join(c for c in raw.decode("latin-1") if c.isprintable()).strip()[:48]
    return name or "unnamed client"


class Registry:
    """Apps seen so far and whether each may connect. Stored in apps.json."""

    def __init__(self, path: Path, default_allow: bool = True):
        self.path = Path(path)
        self.apps: dict[str, dict] = {}
        self.default_allow = default_allow
        if self.path.exists():
            data = json.loads(self.path.read_text() or "{}")
            self.apps = data.get("apps", {})
            self.default_allow = data.get("default_allow", default_allow)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"default_allow": self.default_allow, "apps": self.apps}, indent=2))
        os.replace(tmp, self.path)

    def seen(self, key: str, name: str, ip: str, server: str) -> dict:
        now = time.time()
        app = self.apps.get(key)
        if app is None:
            app = self.apps[key] = {"name": name, "ip": ip, "allowed": self.default_allow, "first_seen": now}
        app.update(last_seen=now, last_server=server)
        self.save()
        return app


@dataclass
class Conn:
    id: int
    server: str
    app: str
    name: str
    ip: str
    since: float = field(default_factory=time.time)
    bytes_down: int = 0
    bytes_up: int = 0
    rate_down: float = 0.0
    freq: int | None = None
    gain: int | None = None
    streaming: bool = False
    _last_bytes: int = 0
    _closers: list = field(default_factory=list, repr=False)

    def public(self) -> dict:
        return {"id": self.id, "server": self.server, "app": self.app, "name": self.name, "ip": self.ip,
                "since": self.since, "bytes_down": self.bytes_down, "rate_down": round(self.rate_down),
                "freq": self.freq, "gain": self.gain, "streaming": self.streaming}

    def close(self) -> None:
        for w in self._closers:
            try:
                w.close()
            except Exception:
                pass


class _CommandWatcher:
    """Follows the client's command stream to learn frequency, gain and streaming state."""

    def __init__(self, conn: Conn):
        self.conn, self.buf, self.broken = conn, bytearray(), False

    def feed(self, data: bytes) -> None:
        if self.broken:
            return
        self.buf += data
        while len(self.buf) >= 8:
            cmd, size = struct.unpack_from("<II", self.buf)
            if size > 1 << 16:                    # not a command stream we understand; just forward
                self.broken = True
                return
            if len(self.buf) < 8 + size:
                return
            body = bytes(self.buf[8:8 + size])
            del self.buf[:8 + size]
            if cmd == CMD_SET and size >= 8:
                setting, value = struct.unpack_from("<II", body)
                if setting == SET_IQ_FREQ or (setting == SET_FFT_FREQ and self.conn.freq is None):
                    self.conn.freq = value
                elif setting == SET_GAIN:
                    self.conn.gain = value
                elif setting == SET_ENABLED:
                    self.conn.streaming = bool(value)


class Switchboard:
    def __init__(self, servers: list[ServerSpec], registry: Registry, listen_host: str = "0.0.0.0"):
        self.servers = {s.key: s for s in servers}
        self.registry = registry
        self.listen_host = listen_host
        self.conns: dict[int, Conn] = {}
        self.events: collections.deque = collections.deque(maxlen=300)
        self.upstream_ok: dict[str, bool] = {s.key: True for s in servers}
        self._ids = itertools.count(1)
        self._listeners: list[asyncio.base_events.Server] = []
        self._ticker: asyncio.Task | None = None

    def event(self, text: str, kind: str = "info") -> None:
        self.events.append({"t": time.time(), "kind": kind, "text": text})
        log.info(text)

    async def start(self) -> None:
        for spec in self.servers.values():
            srv = await asyncio.start_server(lambda r, w, s=spec: self._handle(r, w, s), self.listen_host,
                                             spec.listen_port, reuse_address=True)
            self._listeners.append(srv)
        self._ticker = asyncio.create_task(self._tick())

    async def stop(self) -> None:
        if self._ticker:
            self._ticker.cancel()
        for c in list(self.conns.values()):
            c.close()
        for srv in self._listeners:
            srv.close()
            await srv.wait_closed()

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            for c in self.conns.values():
                c.rate_down, c._last_bytes = c.bytes_down - c._last_bytes, c.bytes_down

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, spec: ServerSpec) -> None:
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        try:
            head = await asyncio.wait_for(reader.readexactly(8), 10)
            cmd, size = struct.unpack("<II", head)
            body = await asyncio.wait_for(reader.readexactly(size), 10) if cmd == CMD_HELLO and size <= 4096 else b""
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        name = clean_name(body[4:]) if cmd == CMD_HELLO else "unnamed client"
        key = f"{name}@{peer}"
        app = self.registry.seen(key, name, peer, spec.key)
        if not app["allowed"]:
            self.event(f"Refused {name} ({peer}) on {spec.name}: switched off", "refused")
            writer.close()
            return
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection(spec.upstream_host, spec.upstream_port), 5)
        except (OSError, asyncio.TimeoutError):
            self.upstream_ok[spec.key] = False
            self.event(f"{spec.name}: SpyServer at {spec.upstream_host}:{spec.upstream_port} is not answering", "error")
            writer.close()
            return
        self.upstream_ok[spec.key] = True
        conn = Conn(next(self._ids), spec.key, key, name, peer)
        conn._closers = [writer, uw]
        self.conns[conn.id] = conn
        self.event(f"{name} ({peer}) connected to {spec.name}", "connect")
        uw.write(head + body)
        watcher = _CommandWatcher(conn)

        async def up():
            while data := await reader.read(65536):
                watcher.feed(data)
                conn.bytes_up += len(data)
                uw.write(data)
                await uw.drain()

        async def down():
            while data := await ur.read(262144):
                conn.bytes_down += len(data)
                writer.write(data)
                await writer.drain()

        tasks = {asyncio.create_task(up()), asyncio.create_task(down())}
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            conn.close()
            self.conns.pop(conn.id, None)
            self.event(f"{name} ({peer}) left {spec.name}", "disconnect")

    # -- control ---------------------------------------------------------------------------------
    def set_allowed(self, key: str, allowed: bool) -> None:
        app = self.registry.apps.get(key)
        if app is None:
            raise KeyError(key)
        app["allowed"] = bool(allowed)
        self.registry.save()
        self.event(f"{app['name']} ({app['ip']}) switched {'on' if allowed else 'off'}")
        if not allowed:
            for c in [c for c in self.conns.values() if c.app == key]:
                c.close()

    def disconnect(self, conn_id: int) -> None:
        c = self.conns.get(conn_id)
        if c is None:
            raise KeyError(conn_id)
        self.event(f"Disconnected {c.name} ({c.ip}) from {self.servers[c.server].name}")
        c.close()

    def forget(self, key: str) -> None:
        if any(c.app == key for c in self.conns.values()):
            raise ValueError("Disconnect the app before forgetting it")
        if self.registry.apps.pop(key, None) is None:
            raise KeyError(key)
        self.registry.save()

    def set_default_allow(self, allow: bool) -> None:
        self.registry.default_allow = bool(allow)
        self.registry.save()

    def snapshot(self) -> dict:
        return {
            "servers": [{"key": s.key, "name": s.name, "port": s.listen_port,
                         "upstream": f"{s.upstream_host}:{s.upstream_port}", "upstream_ok": self.upstream_ok[s.key],
                         "connections": [c.public() for c in self.conns.values() if c.server == s.key]}
                        for s in self.servers.values()],
            "apps": [{"key": k, **{f: v for f, v in a.items()},
                      "connected": [c.server for c in self.conns.values() if c.app == k]}
                     for k, a in sorted(self.registry.apps.items(), key=lambda kv: -kv[1].get("last_seen", 0))],
            "default_allow": self.registry.default_allow,
            "events": list(self.events)[-80:][::-1],
        }
