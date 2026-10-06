"""Channels, recorded transmissions and listening coverage, in SQLite plus audio files on disk.

Recordings are kept as 8 kHz mu-law (8 kB per second of speech) and served as 16-bit WAV.
"""
from __future__ import annotations

import io
import logging
import os
import sqlite3
import threading
import time
import wave
from pathlib import Path

import numpy as np
from scipy import signal

log = logging.getLogger("airdesk.store")
FILE_RATE = 8000
DAY = 86400.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    freq_hz REAL NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',          -- manual | detected
    pinned INTEGER NOT NULL DEFAULT 0,              -- pinned channels are never retired
    squelch_db REAL NOT NULL DEFAULT 8.0,
    created REAL NOT NULL,
    last_heard REAL,
    rate REAL NOT NULL DEFAULT 0,                   -- transmissions per hour of listening
    listened_s REAL NOT NULL DEFAULT 0,
    tx_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'alive'            -- alive | dead
);
CREATE TABLE IF NOT EXISTS transmissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    freq_hz REAL NOT NULL,
    start REAL NOT NULL,
    duration REAL NOT NULL,
    level_db REAL NOT NULL,
    file TEXT,
    bytes INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tx_channel_start ON transmissions(channel_id, start);
CREATE INDEX IF NOT EXISTS tx_start ON transmissions(start);
CREATE TABLE IF NOT EXISTS dwells (
    start REAL NOT NULL,
    end REAL NOT NULL,
    lo REAL NOT NULL,
    hi REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS dwell_end ON dwells(end);
CREATE TABLE IF NOT EXISTS segments (
    idx INTEGER PRIMARY KEY,
    rate REAL NOT NULL,
    listened_s REAL NOT NULL DEFAULT 0,
    last_visit REAL
);
"""


def mulaw_encode(pcm16: np.ndarray) -> bytes:
    x = np.clip(pcm16.astype(np.float64) / 32768.0, -1, 1)
    y = np.sign(x) * np.log1p(255 * np.abs(x)) / np.log1p(255)
    return np.round((y + 1) / 2 * 255).astype(np.uint8).tobytes()


def mulaw_decode(data: bytes) -> np.ndarray:
    y = np.frombuffer(data, np.uint8).astype(np.float64) / 255 * 2 - 1
    x = np.sign(y) * ((1 + 255) ** np.abs(y) - 1) / 255
    return np.clip(np.round(x * 32767), -32767, 32767).astype(np.int16)


def wav_bytes(pcm16: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16.astype("<i2").tobytes())
    return buf.getvalue()


class Store:
    def __init__(self, folder: Path, retention_days: float = 7.0, max_mb: float = 4000.0, clock=time.time):
        self.folder = Path(folder)
        self.rec_dir = self.folder / "recordings"
        self.rec_dir.mkdir(parents=True, exist_ok=True)
        self.retention_s = retention_days * DAY
        self.max_bytes = int(max_mb * 1e6)
        self.clock = clock
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.folder / "airdesk.db", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # -- channels -----------------------------------------------------------------------------------
    def channels(self, status: str = "all") -> list[dict]:
        q = "SELECT * FROM channels" + ("" if status == "all" else " WHERE status = ?") + " ORDER BY freq_hz"
        with self._lock:
            return [dict(r) for r in self.db.execute(q, () if status == "all" else (status,))]

    def channel(self, cid: str) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
        return dict(r) if r else None

    def find_channel(self, freq_hz: float, tolerance: float = 3_000) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM channels WHERE abs(freq_hz - ?) <= ? ORDER BY abs(freq_hz - ?) LIMIT 1",
                                (freq_hz, tolerance, freq_hz)).fetchone()
        return dict(r) if r else None

    def add_channel(self, cid: str, freq_hz: float, label: str = "", source: str = "manual", pinned: bool = False,
                    squelch_db: float = 8.0, rate: float = 0.0) -> dict:
        with self._lock:
            self.db.execute("INSERT INTO channels (id, freq_hz, label, source, pinned, squelch_db, created, rate) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (cid, freq_hz, label, source, int(pinned), squelch_db, self.clock(), rate))
        return self.channel(cid)

    def update_channel(self, cid: str, **fields) -> dict | None:
        allowed = {"label", "pinned", "squelch_db", "freq_hz", "rate", "listened_s", "status", "last_heard"}
        sets = {k: (int(v) if k == "pinned" else v) for k, v in fields.items() if k in allowed}
        if sets:
            with self._lock:
                self.db.execute(f"UPDATE channels SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                                (*sets.values(), cid))
        return self.channel(cid)

    def delete_channel(self, cid: str) -> None:
        with self._lock:
            files = [r["file"] for r in self.db.execute("SELECT file FROM transmissions WHERE channel_id = ?", (cid,))]
            self.db.execute("DELETE FROM transmissions WHERE channel_id = ?", (cid,))
            self.db.execute("DELETE FROM channels WHERE id = ?", (cid,))
        self._unlink(files)

    # -- transmissions ------------------------------------------------------------------------------
    def add_transmission(self, channel_id: str, freq_hz: float, start: float, pcm12k: np.ndarray, rate: int,
                         level_db: float) -> dict:
        audio = signal.resample_poly(pcm12k.astype(np.float64), FILE_RATE, rate) if rate != FILE_RATE else pcm12k
        data = mulaw_encode(np.clip(audio, -32767, 32767))
        day = time.strftime("%Y-%m-%d", time.gmtime(start))
        (self.rec_dir / day).mkdir(exist_ok=True)
        with self._lock:
            cur = self.db.execute("INSERT INTO transmissions (channel_id, freq_hz, start, duration, level_db, bytes) "
                                  "VALUES (?, ?, ?, ?, ?, ?)",
                                  (channel_id, freq_hz, start, len(audio) / FILE_RATE, level_db, len(data)))
            tid = cur.lastrowid
            rel = f"{day}/{tid}.ulaw"
            (self.rec_dir / rel).write_bytes(data)
            self.db.execute("UPDATE transmissions SET file = ? WHERE id = ?", (rel, tid))
            self.db.execute("UPDATE channels SET last_heard = max(coalesce(last_heard, 0), ?), tx_count = tx_count + 1, "
                            "status = 'alive' WHERE id = ?", (start, channel_id))
        return self.transmission(tid)

    def transmission(self, tid: int) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM transmissions WHERE id = ?", (tid,)).fetchone()
        return dict(r) if r else None

    def transmissions(self, channel_id: str | None = None, since: float = 0, limit: int = 500) -> list[dict]:
        q = "SELECT * FROM transmissions WHERE start >= ?" + (" AND channel_id = ?" if channel_id else "")
        q += " ORDER BY start DESC LIMIT ?"
        args = (since, channel_id, limit) if channel_id else (since, limit)
        with self._lock:
            return [dict(r) for r in self.db.execute(q, args)]

    def recent_starts(self, since: float) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        with self._lock:
            for r in self.db.execute("SELECT channel_id, start FROM transmissions WHERE start >= ? ORDER BY start", (since,)):
                out.setdefault(r["channel_id"], []).append(r["start"])
        return out

    def wav(self, tid: int) -> bytes | None:
        t = self.transmission(tid)
        if not t or not t["file"]:
            return None
        try:
            data = (self.rec_dir / t["file"]).read_bytes()
        except OSError:
            return None
        return wav_bytes(mulaw_decode(data), FILE_RATE)

    # -- listening coverage and scanner segments -----------------------------------------------------
    def add_dwell(self, start: float, end: float, lo: float, hi: float) -> None:
        with self._lock:
            self.db.execute("INSERT INTO dwells VALUES (?, ?, ?, ?)", (start, end, lo, hi))

    def coverage(self, freq_hz: float, since: float) -> list[tuple[float, float]]:
        with self._lock:
            rows = self.db.execute("SELECT start, end FROM dwells WHERE end >= ? AND lo <= ? AND hi >= ? ORDER BY start",
                                   (since, freq_hz, freq_hz)).fetchall()
        merged: list[list[float]] = []
        for s, e in rows:
            if merged and s <= merged[-1][1] + 2:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        return [(s, e) for s, e in merged]

    def segments(self) -> dict[int, dict]:
        with self._lock:
            return {r["idx"]: dict(r) for r in self.db.execute("SELECT * FROM segments")}

    def save_segment(self, idx: int, rate: float, listened_s: float, last_visit: float) -> None:
        with self._lock:
            self.db.execute("INSERT INTO segments VALUES (?, ?, ?, ?) ON CONFLICT(idx) DO UPDATE SET "
                            "rate = excluded.rate, listened_s = excluded.listened_s, last_visit = excluded.last_visit",
                            (idx, rate, listened_s, last_visit))

    # -- housekeeping -------------------------------------------------------------------------------
    def maintain(self, retire_after_s: float = DAY) -> dict:
        """Drop old recordings, retire silent detected channels, forget long-dead ones."""
        now = self.clock()
        cutoff = now - self.retention_s
        with self._lock:
            old = [r["file"] for r in self.db.execute("SELECT file FROM transmissions WHERE start < ?", (cutoff,))]
            self.db.execute("DELETE FROM transmissions WHERE start < ?", (cutoff,))
            self.db.execute("DELETE FROM dwells WHERE end < ?", (cutoff,))
            total = self.db.execute("SELECT coalesce(sum(bytes), 0) FROM transmissions").fetchone()[0]
            over = []
            while total > self.max_bytes:
                r = self.db.execute("SELECT id, file, bytes FROM transmissions ORDER BY start LIMIT 1").fetchone()
                if r is None:
                    break
                self.db.execute("DELETE FROM transmissions WHERE id = ?", (r["id"],))
                over.append(r["file"])
                total -= r["bytes"]
            retired = self.db.execute(
                "UPDATE channels SET status = 'dead' WHERE status = 'alive' AND source = 'detected' AND pinned = 0 "
                "AND coalesce(last_heard, created) < ?", (now - retire_after_s,)).rowcount
            forgotten = self.db.execute(
                "DELETE FROM channels WHERE status = 'dead' AND pinned = 0 AND coalesce(last_heard, created) < ? "
                "AND id NOT IN (SELECT DISTINCT channel_id FROM transmissions)", (cutoff,)).rowcount
        self._unlink(old + over)
        merged = self.merge_duplicates()
        self._prune_empty_days()
        return {"expired": len(old), "trimmed": len(over), "retired": retired, "forgotten": forgotten, "merged": merged}

    def merge_duplicates(self, within_hz: float = 25e3, same_time_s: float = 2.5) -> int:
        """Remove found channels that only ever transmitted together with a busier neighbour: one wide
        signal spilling over several raster steps, not separate stations."""
        removed = 0
        with self._lock:
            chans = [dict(r) for r in self.db.execute("SELECT * FROM channels ORDER BY tx_count DESC, freq_hz")]
            starts = {c["id"]: [r[0] for r in self.db.execute("SELECT start FROM transmissions WHERE channel_id = ?", (c["id"],))]
                      for c in chans}
        gone = set()
        for c in chans:
            if c["source"] != "detected" or c["pinned"] or not starts[c["id"]]:
                continue
            for other in chans:
                if other["id"] == c["id"] or other["id"] in gone or abs(other["freq_hz"] - c["freq_hz"]) > within_hz:
                    continue
                if other["tx_count"] < c["tx_count"] or (other["tx_count"] == c["tx_count"] and other["freq_hz"] > c["freq_hz"]):
                    continue
                theirs = starts[other["id"]]
                if all(any(abs(t - u) <= same_time_s for u in theirs) for t in starts[c["id"]]):
                    self.delete_channel(c["id"])
                    gone.add(c["id"])
                    removed += 1
                    break
        return removed

    def usage_bytes(self) -> int:
        with self._lock:
            return int(self.db.execute("SELECT coalesce(sum(bytes), 0) FROM transmissions").fetchone()[0])

    def _unlink(self, files) -> None:
        for f in files:
            if f:
                try:
                    (self.rec_dir / f).unlink()
                except OSError:
                    pass

    def _prune_empty_days(self) -> None:
        for d in self.rec_dir.iterdir():
            if d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                except OSError:
                    pass
