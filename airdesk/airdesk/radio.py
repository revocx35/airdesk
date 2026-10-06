"""The airband radio: one SpyServer stream, cut into voice and data channels.

In scan mode the radio sweeps the airband segment by segment (see scanner.py), finds voice
channels by itself (detector.py) and records every transmission (store.py). In fixed mode it stays
on one window and does the same there.
"""
from __future__ import annotations

import concurrent.futures
import itertools
import json
import logging
import os
import threading
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from sdrcommon.spyserver import SourceError, SpyServerSource

from . import acars, scanner, vdl2
from .detector import AIRBAND, VoiceDetector, snap
from .dsp import AUDIO_RATE, BurstCatcher, Channelizer, Clip, VoiceChannel
from .scanner import Scheduler, Segment, make_segments, smooth
from .store import Store

log = logging.getLogger("airdesk.radio")
ACARS_FREQS = (131.525e6, 131.725e6, 131.825e6)
CHUNK_S = 0.05
USABLE = 0.42                                           # fraction of the sample rate on each side we trust
FIXED_DWELL_S = 300.0                                   # how often fixed mode books its listening time
IGNORE_S = 600.0                                        # how long a frequency that sent a non-voice burst is ignored
MAINTAIN_S = 60.0


@dataclass
class Channel:                                          # kept so old radio.json files still load
    id: str
    freq_hz: float
    label: str = ""
    squelch_db: float = 8.0


@dataclass
class RadioConfig:
    host: str = "127.0.0.1"
    port: int = 5555
    mode: str = "scan"                                  # scan | fixed
    center_hz: float = 136_400_000.0                    # fixed mode window
    gain: int = 22
    running: bool = False
    vdl2: bool = True
    acars: bool = True
    keep_clips: bool = True                             # save recordings of voice transmissions
    channels: list[Channel] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "RadioConfig":
        d = dict(d)
        d["channels"] = [Channel(**c) for c in d.get("channels", [])]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class ConfigStore:
    def __init__(self, path: Path, defaults: RadioConfig):
        self.path = Path(path)
        self.cfg = defaults
        if self.path.exists():
            try:
                self.cfg = RadioConfig.from_json(json.loads(self.path.read_text()))
            except (ValueError, TypeError) as e:
                log.warning("ignoring unreadable %s: %s", self.path, e)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cfg.to_json(), indent=2))
        os.replace(tmp, self.path)


_ids = itertools.count(1)


def new_channel_id() -> str:
    return f"ch{int(time.time() * 1000) % 10_000_000}{next(_ids)}"


@dataclass
class _Provisional:
    """An unknown frequency that just started transmitting: recorded until we know whether it is voice."""
    burst_freq: float
    voice: VoiceChannel
    verdict: str = ""                                    # "" until the burst ends, then "voice" or "other"
    channel_id: str = ""
    clips: list = field(default_factory=list)


class RadioEngine:
    def __init__(self, store: ConfigStore, messages, recordings: Store, client_name: str = "airdesk",
                 source_factory=None, clock=time.time):
        self.store, self.messages, self.rec = store, messages, recordings
        self.client_name = client_name
        self.clock = clock
        self.source_factory = source_factory or (lambda c: SpyServerSource(c.host, c.port, client_name=self.client_name))
        self.state = "stopped"
        self.message = ""
        self.device = ""
        self.sample_rate = 0.0
        self.center_hz = 0.0
        self.spectrum: dict | None = None
        self.audio: deque = deque(maxlen=80)               # (seq, {channel id: int16 array})
        self.audio_seq = 0
        self.vdl2_state = {"enabled": False, "available": vdl2.available(), "freqs": [], "decoded": 0, "dropped": 0}
        self.segments = make_segments()
        saved = recordings.segments()
        for s in self.segments:
            if s.idx in saved:
                s.rate, s.listened_s, s.last_visit = saved[s.idx]["rate"], saved[s.idx]["listened_s"], saved[s.idx]["last_visit"] or 0.0
        self.scheduler = Scheduler(self.segments)
        self._views: list[dict] = []
        self._dwell: tuple[float, float, float] | None = None
        self._channels: list[dict] = []
        self._listen: dict[int, set[str]] = {}
        self._lock = threading.Lock()
        self._changed = threading.Event()
        self._reconnect = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="acars")
        self._tx_listeners: list = []
        self._migrate()
        self.refresh_channels()

    # -- channels (kept in the store; the engine caches the list) --------------------------------------
    def _migrate(self) -> None:
        """Channels from the old radio.json become pinned manual channels in the store."""
        if self.cfg.channels:
            for c in self.cfg.channels:
                if self.rec.find_channel(c.freq_hz) is None:
                    self.rec.add_channel(c.id, c.freq_hz, c.label, "manual", True, c.squelch_db)
            self.cfg.channels = []
            self.store.save()

    def refresh_channels(self) -> None:
        with self._lock:
            self._channels = self.rec.channels()
        self._changed.set()

    def channels(self) -> list[dict]:
        with self._lock:
            return [dict(c) for c in self._channels]

    def add_channel(self, freq_hz: float, label: str = "", squelch_db: float = 8.0) -> dict:
        if not AIRBAND[0] - 1e6 <= freq_hz <= 1800e6:
            raise ValueError("Frequency out of range")
        if self.rec.find_channel(freq_hz, 1e3):
            raise ValueError(f"{freq_hz / 1e6:.3f} MHz is already a channel")
        c = self.rec.add_channel(new_channel_id(), freq_hz, label, "manual", True, squelch_db)
        self.refresh_channels()
        return c

    def update_channel(self, cid: str, **fields) -> dict:
        if self.rec.channel(cid) is None:
            raise KeyError(cid)
        if fields.get("pinned") is True or "label" in fields:
            fields.setdefault("status", "alive")
        c = self.rec.update_channel(cid, **fields)
        self.refresh_channels()
        return c

    def delete_channel(self, cid: str) -> None:
        if self.rec.channel(cid) is None:
            raise KeyError(cid)
        self.rec.delete_channel(cid)
        self.refresh_channels()

    # -- control --------------------------------------------------------------------------------------
    @property
    def cfg(self) -> RadioConfig:
        return self.store.cfg

    def on_transmission(self, fn) -> None:
        self._tx_listeners.append(fn)

    def off_transmission(self, fn) -> None:
        if fn in self._tx_listeners:
            self._tx_listeners.remove(fn)

    def set_listening(self, viewer: int, channel_ids: set[str]) -> None:
        with self._lock:
            if channel_ids:
                self._listen[viewer] = set(channel_ids)
            else:
                self._listen.pop(viewer, None)

    def listening(self) -> set[str]:
        with self._lock:
            return set().union(*self._listen.values()) if self._listen else set()

    def start(self) -> None:
        self.cfg.running = True
        self.store.save()
        if self._thread and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.cfg.running = False
        self.store.save()
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        with self._lock:
            self.state, self.message, self._views, self.spectrum = "stopped", "", [], None

    def shutdown(self) -> None:
        """Stop the worker without changing whether the radio should run next time."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._pool.shutdown(wait=False)

    def update(self, **changes) -> None:
        cfg = self.cfg
        if any(k in changes and changes[k] != getattr(cfg, k) for k in ("host", "port")):
            self._reconnect.set()
        for k, v in changes.items():
            if k in ("host", "port", "center_hz", "gain", "vdl2", "acars", "keep_clips", "mode"):
                setattr(cfg, k, v)
        self.store.save()
        self._changed.set()

    def half_width(self) -> float:
        return USABLE * (self.sample_rate or 2.4e6)

    def current_dwell(self) -> tuple[float, float, float] | None:
        """(start, lo, hi) of the listening stretch in progress, which is booked only when it ends."""
        with self._lock:
            return self._dwell if self.state == "running" else None

    # -- worker ---------------------------------------------------------------------------------------
    def _run(self, stop: threading.Event) -> None:
        delay = 2.0
        while not stop.is_set():
            try:
                self._session(stop)
                delay = 2.0
            except SourceError as e:
                with self._lock:
                    self.state, self.message = "error", str(e)
                log.info("radio: %s", e)
            except Exception as e:                      # keep the app alive and say what broke
                log.exception("radio failed")
                with self._lock:
                    self.state, self.message = "error", f"Unexpected error: {e}"
            if stop.wait(delay):
                break
            delay = min(delay * 2, 30.0)
            with self._lock:
                if self.state == "error":
                    self.message += f" Retrying in {int(delay)} s."

    def _session(self, stop: threading.Event) -> None:
        cfg = self.cfg
        with self._lock:
            self.state, self.message = "connecting", ""
        src = self.source_factory(cfg)
        src.open()
        bridge = vdl2.Vdl2Bridge(lambda m: self.messages.add(m))
        S = _Session(self, src, bridge)
        try:
            self.sample_rate = src.sample_rate
            with self._lock:
                self.device = src.description
            src.set_gain(cfg.gain)
            src.start()
            S.go_to(S.next_center(self.clock()), first=True)
            with self._lock:
                self.state, self.message = "running", ""
            while not stop.is_set():
                if self._reconnect.is_set():
                    self._reconnect.clear()
                    return
                if not S.step():
                    return
        finally:
            S.close()
            bridge.stop()
            src.close()

    def _emit(self, tx: dict) -> None:
        for fn in list(self._tx_listeners):
            try:
                fn(tx)
            except Exception:
                pass

    # -- views ------------------------------------------------------------------------------------------
    def snapshot(self) -> dict:
        shares = self.scheduler.shares()
        cur = self.scheduler.current
        now = self.clock()
        center = self.center_hz or self.cfg.center_hz
        hw = self.half_width()
        with self._lock:
            views = [dict(v) for v in self._views]
            chans = [dict(c) for c in self._channels]
        if not views:
            views = [{"id": c["id"], "freq_hz": c["freq_hz"], "label": c["label"], "kind": "voice",
                      "inside": False, "level_db": 0.0, "open": False} for c in chans if c["status"] == "alive"]
        return {"state": self.state, "message": self.message, "device": self.device,
                "config": {k: v for k, v in self.cfg.to_json().items() if k != "channels"},
                "channels": views, "window": [center - hw, center + hw], "vdl2": dict(self.vdl2_state),
                "scanner": {"segments": [s.public(shares[s.idx]) for s in self.segments],
                            "current": cur.idx if cur and self.cfg.mode == "scan" and self.state == "running" else None,
                            "dwell_s": round(now - self.scheduler.dwell_start, 1) if cur else 0,
                            "holding": bool(self.listening())},
                "recordings": {"bytes": self.rec.usage_bytes(), "days": self.rec.retention_s / 86400},
                "counts": {"alive": sum(c["status"] == "alive" for c in chans),
                           "dead": sum(c["status"] == "dead" for c in chans)}}

    def audio_since(self, seq: int) -> list[tuple[int, dict]]:
        with self._lock:
            return [(s, f) for s, f in self.audio if s > seq]


class _Session:
    """One connection to the radio: window handling, detection, recording and scanning."""

    def __init__(self, engine: RadioEngine, src, bridge):
        self.e, self.src, self.bridge = engine, src, bridge
        self.fs = src.sample_rate
        self.chz = Channelizer(self.fs)
        self.det = VoiceDetector(self.fs)
        self.voices: dict[str, VoiceChannel] = {}
        self.prov: dict[str, _Provisional] = {}          # provisional id -> state
        self.bursts: dict[str, tuple[BurstCatcher, float]] = {}
        self.floors: dict[str, float] = {}
        self.ignored: dict[float, float] = {}
        self.dwell_tx: Counter = Counter()
        self.dwell_start = 0.0
        self.center = 0.0
        self.t_spec = self.t_maint = 0.0
        self.chunk = int(self.fs * CHUNK_S)
        self.gain = engine.cfg.gain

    # -- windows -----------------------------------------------------------------------------------------
    def next_center(self, now: float) -> float:
        e = self.e
        if e.cfg.mode != "scan":
            return e.cfg.center_hz
        return e.scheduler.pick(now, self.hold_segment()).center

    def hold_segment(self) -> Segment | None:
        for cid in self.e.listening():
            c = next((c for c in self.e.channels() if c["id"] == cid), None)
            if c:
                return self.e.scheduler.segment_for(c["freq_hz"])
        return None

    def go_to(self, center: float, first: bool = False) -> None:
        if first or abs(center - self.src.center_hz) > 1:
            self.src.tune(center)
            self.src.discard(0.1)
        self.center = self.e.center_hz = center
        for b in self.det.set_window(center, self.e.half_width(), self.e.clock()):   # cut-off bursts
            self._burst_ended(b)
        self.dwell_start = self.e.clock()
        self.dwell_tx.clear()
        self._publish_dwell()
        self.rebuild()

    def _publish_dwell(self) -> None:
        hw = self.e.half_width()
        with self.e._lock:
            self.e._dwell = (self.dwell_start, self.center - hw, self.center + hw)

    def close_dwell(self, now: float) -> None:
        """Book the listening time: coverage, channel scores and (in scan mode) the segment score."""
        e = self.e
        dur = now - self.dwell_start
        if dur <= 0.5:
            return
        hw = e.half_width()
        lo, hi = max(self.center - hw, 0), self.center + hw
        e.rec.add_dwell(self.dwell_start, now, lo, hi)
        for c in e.channels():
            if c["status"] == "alive" and lo <= c["freq_hz"] <= hi:
                n = self.dwell_tx.get(c["id"], 0)
                e.rec.update_channel(c["id"], rate=smooth(c["rate"], n / (dur / 3600), dur), listened_s=c["listened_s"] + dur)
        if e.cfg.mode == "scan":
            seg = e.scheduler.finish(now)
            if seg:
                e.rec.save_segment(seg.idx, seg.rate, seg.listened_s, seg.last_visit)
        e.refresh_channels()
        self.dwell_start = now
        self.dwell_tx.clear()
        self._publish_dwell()

    def rebuild(self) -> None:
        """(Re)create the channel decoders for the current window."""
        e, cfg = self.e, self.e.cfg
        hw = e.half_width()
        lo, hi = self.center - hw, self.center + hw
        for cid, v in self.voices.items():
            if v.floor is not None:
                self.floors[cid] = v.floor
        old = self.voices
        self.voices, views, offsets = {}, [], {}
        for c in e.channels():
            inside = lo <= c["freq_hz"] <= hi
            if c["status"] != "alive":
                continue
            views.append({"id": c["id"], "freq_hz": c["freq_hz"], "label": c["label"], "kind": "voice", "inside": inside,
                          "squelch_db": c["squelch_db"], "level_db": 0.0, "open": False, "source": c["source"]})
            if inside:
                v = old.get(c["id"]) if c["id"] in old and old[c["id"]].freq_hz == c["freq_hz"] else None
                if v is None:
                    v = VoiceChannel(c["id"], c["freq_hz"], c["squelch_db"])
                    v.floor = self.floors.get(c["id"])
                v.squelch_db = c["squelch_db"]
                self.voices[c["id"]] = v
                offsets[c["id"]] = c["freq_hz"] - self.center
        for pid, p in self.prov.items():
            if lo <= p.voice.freq_hz <= hi:
                self.voices[pid] = p.voice
                offsets[pid] = p.voice.freq_hz - self.center
                views.append({"id": pid, "freq_hz": p.voice.freq_hz, "label": "New?", "kind": "voice", "inside": True,
                              "level_db": 0.0, "open": False, "source": "provisional"})
        old_bursts, self.bursts = self.bursts, {}
        if cfg.acars:
            for f in ACARS_FREQS:
                cid = f"acars-{int(f)}"
                inside = lo <= f <= hi
                views.append({"id": cid, "freq_hz": f, "label": "ACARS", "kind": "acars", "inside": inside,
                              "level_db": 0.0, "open": False})
                if inside:                               # keep a catcher that may be in the middle of a burst
                    self.bursts[cid] = old_bursts.get(cid) or (BurstCatcher(self.chz.out_rate), f)
                    offsets[cid] = f - self.center
        self.chz.set_channels(offsets)
        vfreqs = [f for f in vdl2.VDL2_FREQS if lo <= f <= hi]
        if cfg.vdl2 and vfreqs and vdl2.available():
            if not self.bridge.running or self.bridge.freqs != vfreqs or self.bridge.center != self.center:
                self.bridge.start(self.fs, self.center, vfreqs)
        else:
            self.bridge.stop()
        for f in vdl2.VDL2_FREQS:
            views.append({"id": f"vdl2-{int(f)}", "freq_hz": f, "label": "VDL2", "kind": "vdl2", "inside": lo <= f <= hi,
                          "level_db": 0.0, "open": False})
        with e._lock:
            e._views = views
            e.vdl2_state.update(enabled=self.bridge.running, freqs=vfreqs)

    # -- the loop --------------------------------------------------------------------------------------
    def step(self) -> bool:
        e, cfg = self.e, self.e.cfg
        now = e.clock()
        if e._changed.is_set():
            e._changed.clear()
            if cfg.gain != self.gain:
                self.src.set_gain(cfg.gain)
                self.gain = cfg.gain
            if cfg.mode == "fixed" and abs(cfg.center_hz - self.center) > 1:
                self.close_dwell(now)
                self.go_to(cfg.center_hz)
            elif cfg.mode == "scan" and e.scheduler.current is None:
                self.close_dwell(now)
                self.go_to(self.next_center(now))
            else:
                self.rebuild()
        if cfg.mode != "scan" and e.scheduler.current is not None:
            e.scheduler.current = None                    # left scan mode: the scanner's dwell is over
        if cfg.mode == "scan":
            hold = self.hold_segment()
            cur = e.scheduler.current
            # a transmission in progress holds the scanner, unless it is a broadcast that never stops
            transmitting = any(v.open and now - v._clip_start < scanner.LONG_TX_S for v in self.voices.values()) or \
                any(now - b.start < scanner.LONG_TX_S for b in self.det.active.values())
            if (hold is not None and hold is not cur) or e.scheduler.should_move(now, transmitting, hold is cur and hold is not None):
                self.close_dwell(now)
                nxt = e.scheduler.pick(now, hold)
                self.go_to(nxt.center)
        elif now - self.dwell_start >= FIXED_DWELL_S:
            self.close_dwell(now)
        x = self.src.read(self.chunk)
        now = e.clock()
        if now - self.t_spec >= 0.25:
            self.spectrum(x, now)
            self.t_spec = now
        started, ended = self.det.process(x, now, CHUNK_S)
        for b in started:
            self._burst_started(b)
        for b in ended:
            self._burst_ended(b)
        outs = self.chz.process(x)
        frame = {}
        for cid, v in list(self.voices.items()):
            pcm, clip = v.process(outs.get(cid, np.zeros(0, np.complex64)), now)
            frame[cid] = pcm
            if clip is not None:
                self._clip(cid, clip)
        for cid, (catcher, freq) in self.bursts.items():
            b = catcher.process(outs.get(cid, np.zeros(0, np.complex64)))
            if b is not None:
                e._pool.submit(self._decode_acars, b, freq, catcher.level_db - (catcher.floor or 0))
        self.bridge.feed(x)
        with e._lock:
            e.audio_seq += 1
            e.audio.append((e.audio_seq, frame))
            for view in e._views:
                obj = self.voices.get(view["id"]) or (self.bursts.get(view["id"]) or (None,))[0]
                if obj is not None:
                    view["level_db"] = round(obj.level_db - (obj.floor if obj.floor is not None else obj.level_db), 1)
                    view["open"] = bool(getattr(obj, "open", False))
            e.vdl2_state.update(decoded=self.bridge.decoded, dropped=self.bridge.dropped)
        if now - self.t_maint >= MAINTAIN_S:
            self.t_maint = now
            if self.t_maint and any(e.rec.maintain().values()):
                e.refresh_channels()
        return True

    # -- detection -------------------------------------------------------------------------------------
    def _burst_started(self, b) -> None:
        e = self.e
        if self.ignored.get(b.freq, 0) > e.clock():
            return
        known = e.rec.find_channel(b.freq)
        if known is not None:
            if known["status"] == "dead":                 # a retired channel speaks again: bring it back
                e.rec.update_channel(known["id"], status="alive")
                e.refresh_channels()
                v = VoiceChannel(known["id"], known["freq_hz"], known["squelch_db"])
                v.prime(b.snr_db)
                self.voices[known["id"]] = v
                self.rebuild()
            return
        pid = f"new-{int(snap(b.freq))}"
        if pid in self.prov:
            return
        for cid, v in self.voices.items():                 # a known channel next door is talking: it is that one
            if v.open and abs(v.freq_hz - b.freq) <= 25e3:
                return
        v = VoiceChannel(pid, snap(b.freq), 8.0)
        v.prime(b.snr_db)
        self.prov[pid] = _Provisional(b.freq, v)
        self.rebuild()

    def _burst_ended(self, b) -> None:
        e = self.e
        if b.is_voice and b.duration < scanner.LONG_TX_S:
            e.scheduler.voice_heard(e.clock())
        pid = f"new-{int(snap(b.freq))}"
        p = self.prov.get(pid)
        if p is None or p.verdict:
            return
        if b.is_voice and e.rec.find_channel(p.voice.freq_hz) is None:
            c = e.rec.add_channel(new_channel_id(), p.voice.freq_hz, "", "detected", False, 8.0, rate=0.0)
            p.verdict, p.channel_id = "voice", c["id"]
            log.info("found a voice channel on %.3f MHz", p.voice.freq_hz / 1e6)
            e.refresh_channels()
        else:
            p.verdict = "other"
            if not b.cut:                                 # cut short by a retune proves nothing
                self.ignored[b.freq] = e.clock() + IGNORE_S
        if not p.voice.open:
            self._settle(pid)

    def _settle(self, pid: str) -> None:
        """A provisional channel's verdict is in and its squelch has closed: keep or drop its recording."""
        p = self.prov.pop(pid, None)
        if p is None:
            return
        if p.verdict == "voice":
            if p.voice.floor is not None:
                self.floors[p.channel_id] = p.voice.floor
            for clip in p.clips:
                clip.channel = p.channel_id
                self._store_clip(p.channel_id, clip)
        self.rebuild()

    def _clip(self, cid: str, clip: Clip) -> None:
        p = self.prov.get(cid)
        if p is not None:
            p.clips.append(clip)
            if p.verdict:
                self._settle(cid)
            return
        self.dwell_tx[cid] += 1
        self._store_clip(cid, clip)

    def _store_clip(self, cid: str, clip: Clip) -> None:
        e = self.e
        if e.cfg.keep_clips:
            tx = e.rec.add_transmission(cid, clip.freq_hz, clip.start, clip.audio, AUDIO_RATE, clip.level_db)
        else:
            e.rec.update_channel(cid, last_heard=clip.start)
            tx = {"id": None, "channel_id": cid, "freq_hz": clip.freq_hz, "start": clip.start,
                  "duration": clip.duration, "level_db": clip.level_db, "file": None}
        e.refresh_channels()
        e._emit(tx)

    def _decode_acars(self, burst: np.ndarray, freq: float, level: float) -> None:
        for m in acars.Demodulator(AUDIO_RATE).decode(burst):
            self.e.messages.add({"source": "ACARS", "freq_hz": freq, "level_db": round(level, 1), "hex": "",
                                 **m.as_dict()})

    def spectrum(self, x: np.ndarray, now: float) -> None:
        n = 2048
        seg = x[: (len(x) // n) * n].reshape(-1, n) * np.hanning(n).astype(np.float32)
        p = np.fft.fftshift((np.abs(np.fft.fft(seg, axis=1)) ** 2).mean(0))
        db = 10 * np.log10(p + 1e-12)
        small = db.reshape(-1, 4).max(1)
        with self.e._lock:
            self.e.spectrum = {"center_hz": self.center, "span_hz": self.fs, "db": np.round(small - small.max(), 1).tolist()}

    def close(self) -> None:
        try:
            self.close_dwell(self.e.clock())
        except Exception:
            log.exception("could not book the last dwell")
        with self.e._lock:
            self.e._views = []
