"""Decoded data messages (ACARS and VDL2), linked to aircraft in view."""
from __future__ import annotations

import collections
import itertools
import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("airdesk.messages")


class MessageLog:
    """Recent messages in memory; with a path, also appended to a JSON-lines file and reloaded on start."""

    def __init__(self, find_aircraft=lambda reg="", hexid="": "", maxlen: int = 1000, path: Path | None = None):
        self.find = find_aircraft
        self.maxlen = maxlen
        self.path = Path(path) if path else None
        self.items: collections.deque = collections.deque(maxlen=maxlen)
        self.counts = collections.Counter()
        self._lock = threading.Lock()
        self._listeners: list = []
        self._lines = 0
        start = 1
        if self.path and self.path.exists():
            try:
                lines = self.path.read_text().splitlines()
                for line in lines[-maxlen:]:
                    m = json.loads(line)
                    self.items.append(m)
                    self.counts[m.get("source", "?")] += 1
                self._lines = len(lines)
                start = max((m.get("id", 0) for m in self.items), default=0) + 1
            except (OSError, ValueError) as e:
                log.warning("could not read %s: %s", self.path, e)
        self._ids = itertools.count(start)

    def subscribe(self, fn) -> None:
        self._listeners.append(fn)

    def unsubscribe(self, fn) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def add(self, msg: dict) -> dict:
        m = dict(msg)
        m["t"] = m.get("t") or time.time()
        m["aircraft"] = self.find(reg=m.get("reg", ""), hexid=m.get("hex", "")) or m.get("hex", "").lower()
        with self._lock:
            m["id"] = next(self._ids)
            self.items.append(m)
            self.counts[m.get("source", "?")] += 1
            if self.path:
                self._persist(m)
        for fn in list(self._listeners):
            try:
                fn(m)
            except Exception:
                pass
        return m

    def _persist(self, m: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self._lines >= 5 * self.maxlen:              # compact: keep what is in memory
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text("".join(json.dumps(x) + "\n" for x in self.items))
                tmp.replace(self.path)
                self._lines = len(self.items)
            else:
                with self.path.open("a") as f:
                    f.write(json.dumps(m) + "\n")
                self._lines += 1
        except OSError as e:
            log.warning("could not save message: %s", e)

    def list(self, aircraft: str = "", limit: int = 200) -> list[dict]:
        with self._lock:
            items = [m for m in self.items if not aircraft or m.get("aircraft") == aircraft.lower()]
        return items[-limit:][::-1]
