"""Aircraft from an existing readsb/tar1090 installation, read-only over HTTP."""
from __future__ import annotations

import collections
import gzip
import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("airdesk.adsb")


def _get(url: str, timeout: float = 5.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "airdesk"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


class Tar1090Db:
    """Registration and type from tar1090's aircraft database (files split by ICAO-address prefix).

    Each file maps the rest of an address to [registration, type code, flags, description] and lists
    'children': longer prefixes that live in their own files.
    """

    def __init__(self, base_url: str, fetch=_get):
        self.base, self.fetch = base_url.rstrip("/"), fetch
        self.folder: str | None = None
        self._files: dict[str, dict | None] = {}
        self._cache: dict[str, dict | None] = {}
        self._retry_at = 0.0
        self._lock = threading.Lock()

    def _discover(self) -> bool:
        if self.folder:
            return True
        if time.time() < self._retry_at:
            return False
        try:
            page = self.fetch(self.base + "/").decode("utf-8", "replace")
            m = re.search(r'databaseFolder\s*=\s*"([^"]+)"', page)
            self.folder = m.group(1) if m else None
        except (OSError, urllib.error.URLError, ValueError) as e:
            log.info("aircraft database not available: %s", e)
        if not self.folder:
            self._retry_at = time.time() + 600
        return bool(self.folder)

    def _file(self, key: str) -> dict | None:
        if key not in self._files:
            try:
                self._files[key] = json.loads(self.fetch(f"{self.base}/{self.folder}/{key}.js"))
            except (OSError, urllib.error.URLError, ValueError):
                self._files[key] = None
        return self._files[key]

    def lookup(self, hexid: str) -> dict | None:
        hexid = hexid.upper().lstrip("~")
        with self._lock:
            if hexid in self._cache:
                return self._cache[hexid]
            result = None
            if len(hexid) == 6 and self._discover():
                key = hexid[0]
                while True:
                    d = self._file(key)
                    if d is None:
                        break
                    rec = d.get(hexid[len(key):])
                    if rec:
                        result = {"reg": rec[0] or "", "type": rec[1] or "", "desc": (rec[3] if len(rec) > 3 else "") or ""}
                        break
                    nxt = [c for c in d.get("children", []) if hexid.startswith(c)]
                    if not nxt:
                        break
                    key = max(nxt, key=len)
            self._cache[hexid] = result
            return result


def distance_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


class AdsbFeed:
    TRAIL_POINTS = 240
    FORGET_S = 90

    def __init__(self, base_url: str, receiver: tuple[float, float] | None = None, db: Tar1090Db | None = None,
                 fetch=_get, interval: float = 1.0):
        self.base, self.fetch, self.interval = base_url.rstrip("/"), fetch, interval
        self.db = db if db is not None else Tar1090Db(self.base, fetch)
        self.receiver = receiver
        self.aircraft: dict[str, dict] = {}
        self.trails: dict[str, collections.deque] = {}
        self.status = {"ok": False, "error": "Not started", "updated": 0.0, "messages": 0}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        if self.receiver is None:
            try:
                r = json.loads(self.fetch(self.base + "/data/receiver.json"))
                if "lat" in r and "lon" in r:
                    self.receiver = (float(r["lat"]), float(r["lon"]))
            except (OSError, urllib.error.URLError, ValueError):
                pass
        while not self._stop.is_set():
            try:
                self.update(json.loads(self.fetch(self.base + "/data/aircraft.json")))
            except (OSError, urllib.error.URLError, ValueError) as e:
                with self._lock:
                    self.status.update(ok=False, error=f"Cannot read {self.base}/data/aircraft.json ({e})")
            self._stop.wait(self.interval)

    def update(self, data: dict) -> None:
        now = float(data.get("now") or time.time())
        seen_hex = set()
        for a in data.get("aircraft", []):
            hexid = str(a.get("hex", "")).lower()
            if not hexid or a.get("seen", 99) > 60:
                continue
            seen_hex.add(hexid)
            info = self.db.lookup(hexid) or {}
            alt = a.get("alt_baro", a.get("alt_geom"))
            rec = {
                "hex": hexid, "flight": (a.get("flight") or "").strip(),
                "reg": a.get("r") or info.get("reg", ""), "type": a.get("t") or info.get("type", ""),
                "desc": a.get("desc") or info.get("desc", ""),
                "alt": alt, "gs": a.get("gs"), "track": a.get("track", a.get("true_heading")),
                "vr": a.get("baro_rate", a.get("geom_rate")), "squawk": a.get("squawk", ""),
                "category": a.get("category", ""), "seen": a.get("seen"), "rssi": a.get("rssi"),
                "emergency": a.get("emergency", "none"), "mlat": "lat" in (a.get("mlat") or []),
                "messages": a.get("messages"),
            }
            if "lat" in a and "lon" in a and a.get("seen_pos", 99) < 60:
                rec.update(lat=a["lat"], lon=a["lon"], seen_pos=a.get("seen_pos"))
                if self.receiver:
                    rec["dist_km"] = round(distance_km(self.receiver[0], self.receiver[1], a["lat"], a["lon"]), 1)
            with self._lock:
                self.aircraft[hexid] = {**rec, "_t": now}
                if "lat" in rec:
                    tr = self.trails.setdefault(hexid, collections.deque(maxlen=self.TRAIL_POINTS))
                    pt = (round(rec["lat"], 5), round(rec["lon"], 5), alt if isinstance(alt, (int, float)) else 0)
                    if not tr or tr[-1][:2] != pt[:2]:
                        tr.append(pt)
        with self._lock:
            for h in [h for h, r in self.aircraft.items() if h not in seen_hex and now - r["_t"] > self.FORGET_S]:
                self.aircraft.pop(h, None)
                self.trails.pop(h, None)
            self.status.update(ok=True, error="", updated=time.time(), messages=data.get("messages", 0))

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [{k: v for k, v in r.items() if not k.startswith("_")} for r in self.aircraft.values()]

    def trails_snapshot(self) -> dict[str, list]:
        with self._lock:
            return {h: list(t) for h, t in self.trails.items() if h in self.aircraft}

    def find(self, reg: str = "", hexid: str = "") -> str:
        """Address of an aircraft in view by ICAO address or registration, or ''."""
        with self._lock:
            if hexid and hexid.lower() in self.aircraft:
                return hexid.lower()
            if reg:
                r = reg.replace("-", "").upper()
                for h, a in self.aircraft.items():
                    if a.get("reg") and a["reg"].replace("-", "").upper() == r:
                        return h
        return ""

    def status_snapshot(self) -> dict:
        with self._lock:
            return {**self.status, "aircraft": len(self.aircraft),
                    "with_position": sum("lat" in a for a in self.aircraft.values()),
                    "receiver": list(self.receiver) if self.receiver else None}
