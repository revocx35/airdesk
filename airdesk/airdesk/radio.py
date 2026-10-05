"""The airband radio: one SpyServer stream, cut into voice and data channels."""
from __future__ import annotations

import collections
import concurrent.futures
import itertools
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from sdrcommon.spyserver import SourceError, SpyServerSource

from . import acars, vdl2
from .dsp import AUDIO_RATE, BurstCatcher, Channelizer, Clip, VoiceChannel

log = logging.getLogger("airdesk.radio")
ACARS_FREQS = (131.525e6, 131.725e6, 131.825e6)
CHUNK_S = 0.05
USABLE = 0.42                                           # fraction of the sample rate on each side we trust


@dataclass
class Channel:
    id: str
    freq_hz: float
    label: str = ""
    squelch_db: float = 8.0


@dataclass
class RadioConfig:
    host: str = "127.0.0.1"
    port: int = 5555
    center_hz: float = 136_400_000.0
    gain: int = 22
    running: bool = False
    vdl2: bool = True
    acars: bool = True
    keep_clips: bool = True
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


class ActivityFinder:
    """Remembers which frequencies in the window carried signals recently, to suggest channels."""

    def __init__(self, keep_s: float = 1800):
        self.keep_s = keep_s
        self.events: dict[float, collections.deque] = {}
        self._active: set[float] = set()
        self.floor: np.ndarray | None = None

    def reset(self) -> None:
        self.events.clear()
        self._active.clear()
        self.floor = None

    def update(self, db: np.ndarray, center: float, fs: float, now: float) -> None:
        if self.floor is None or len(self.floor) != len(db):
            self.floor = db.copy()
            return
        below = db < self.floor + 3
        self.floor = np.where(below, self.floor + 0.05 * (db - self.floor), self.floor + 0.002 * (db - self.floor))
        f = center + (np.arange(len(db)) - len(db) / 2) * fs / len(db)
        hot = (db > self.floor + 12) & (np.abs(f - center) > 15e3) & (np.abs(f - center) < USABLE * fs)
        now_active = set()
        for fr in f[hot]:
            key = round(fr / 5e3) * 5e3                       # 5 kHz grid fits both 25 and 8.33 kHz channels
            now_active.add(key)
            if key not in self._active and key - 5e3 not in self._active and key + 5e3 not in self._active:
                self.events.setdefault(key, collections.deque(maxlen=500)).append(now)
        self._active = now_active

    def top(self, now: float, n: int = 20) -> list[dict]:
        out = []
        for key, ev in list(self.events.items()):
            while ev and now - ev[0] > self.keep_s:
                ev.popleft()
            if not ev:
                del self.events[key]
                continue
            out.append({"freq_hz": key, "count": len(ev), "last": ev[-1]})
        out.sort(key=lambda x: (-x["count"], -x["last"]))
        merged = []                                            # one entry per channel, not per 5 kHz step
        for e in out:
            if all(abs(e["freq_hz"] - m["freq_hz"]) > 6e3 for m in merged):
                merged.append(e)
        return merged[:n]


class RadioEngine:
    def __init__(self, store: ConfigStore, messages, clips_max: int = 300, client_name: str = "airdesk",
                 source_factory=None):
        self.store, self.messages = store, messages
        self.client_name = client_name
        self.source_factory = source_factory or (lambda c: SpyServerSource(c.host, c.port, client_name=self.client_name))
        self.state = "stopped"                          # stopped | connecting | running | error
        self.message = ""
        self.device = ""
        self.sample_rate = 0.0
        self.channels_view: list[dict] = []
        self.spectrum: dict | None = None
        self.clips: collections.deque = collections.deque(maxlen=clips_max)
        self.audio: collections.deque = collections.deque(maxlen=80)     # (seq, {channel id: int16 array})
        self.audio_seq = 0
        self.activity = ActivityFinder()
        self.vdl2_state = {"enabled": False, "available": vdl2.available(), "freqs": [], "decoded": 0, "dropped": 0}
        self._lock = threading.Lock()
        self._changed = threading.Event()
        self._reconnect = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="acars")
        self._clip_listeners: list = []

    # -- control -------------------------------------------------------------------------------
    @property
    def cfg(self) -> RadioConfig:
        return self.store.cfg

    def on_clip(self, fn) -> None:
        self._clip_listeners.append(fn)

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
            self.state, self.message, self.channels_view, self.spectrum = "stopped", "", [], None

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
            if k in ("host", "port", "center_hz", "gain", "vdl2", "acars", "keep_clips"):
                setattr(cfg, k, v)
        self.store.save()
        self._changed.set()

    def set_channels(self, channels: list[Channel]) -> None:
        self.cfg.channels = channels
        self.store.save()
        self._changed.set()

    def window(self) -> tuple[float, float]:
        fs = self.sample_rate or 2.4e6
        return self.cfg.center_hz - USABLE * fs, self.cfg.center_hz + USABLE * fs

    # -- worker --------------------------------------------------------------------------------
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
        try:
            self.sample_rate = fs = src.sample_rate
            with self._lock:
                self.device = src.description
            src.tune(cfg.center_hz)
            src.set_gain(cfg.gain)
            src.start()
            src.discard(0.2)
            chz = Channelizer(fs)
            voices, bursts = self._build(cfg, chz, bridge, fs)
            self.activity.reset()
            with self._lock:
                self.state, self.message = "running", ""
            chunk = int(fs * CHUNK_S)
            t_spec = 0.0
            while not stop.is_set():
                if self._reconnect.is_set():                 # another server: start a fresh session
                    self._reconnect.clear()
                    return
                if self._changed.is_set():
                    self._changed.clear()
                    if src.center_hz != cfg.center_hz:
                        src.tune(cfg.center_hz)
                        src.discard(0.1)
                        self.activity.reset()
                    src.set_gain(cfg.gain)
                    voices, bursts = self._build(cfg, chz, bridge, fs)
                x = src.read(chunk)
                now = time.time()
                if now - t_spec >= 0.25:
                    self._spectrum(x, fs, cfg.center_hz, now)
                    t_spec = now
                outs = chz.process(x)
                frame = {}
                for cid, v in voices.items():
                    pcm, clip = v.process(outs.get(cid, np.zeros(0, np.complex64)), now)
                    frame[cid] = pcm
                    if clip is not None and cfg.keep_clips:
                        self._add_clip(clip)
                for cid, (catcher, freq) in bursts.items():
                    b = catcher.process(outs.get(cid, np.zeros(0, np.complex64)))
                    if b is not None:
                        self._pool.submit(self._decode_acars, b, freq, catcher.level_db - (catcher.floor or 0))
                bridge.feed(x)
                with self._lock:
                    self.audio_seq += 1
                    self.audio.append((self.audio_seq, frame))
                    for view in self.channels_view:
                        obj = voices.get(view["id"]) or (bursts.get(view["id"]) or (None,))[0]
                        if obj is not None:
                            view["level_db"] = round(obj.level_db - (obj.floor or obj.level_db), 1)
                            view["open"] = bool(getattr(obj, "open", False))
                    self.vdl2_state.update(decoded=bridge.decoded, dropped=bridge.dropped)
        finally:
            bridge.stop()
            src.close()

    def _build(self, cfg: RadioConfig, chz: Channelizer, bridge, fs: float):
        lo, hi = cfg.center_hz - USABLE * fs, cfg.center_hz + USABLE * fs
        voices, bursts, views, offsets = {}, {}, [], {}
        for ch in cfg.channels:
            inside = lo <= ch.freq_hz <= hi
            views.append({"id": ch.id, "freq_hz": ch.freq_hz, "label": ch.label, "kind": "voice", "inside": inside,
                          "squelch_db": ch.squelch_db, "level_db": 0.0, "open": False})
            if inside:
                voices[ch.id] = VoiceChannel(ch.id, ch.freq_hz, ch.squelch_db)
                offsets[ch.id] = ch.freq_hz - cfg.center_hz
        if cfg.acars:
            for f in ACARS_FREQS:
                cid = f"acars-{int(f)}"
                inside = lo <= f <= hi
                views.append({"id": cid, "freq_hz": f, "label": "ACARS", "kind": "acars", "inside": inside,
                              "level_db": 0.0, "open": False})
                if inside:
                    bursts[cid] = (BurstCatcher(chz.out_rate), f)
                    offsets[cid] = f - cfg.center_hz
        chz.set_channels(offsets)
        vfreqs = [f for f in vdl2.VDL2_FREQS if lo <= f <= hi]
        if cfg.vdl2 and vfreqs and vdl2.available():
            if not bridge.running or bridge.freqs != vfreqs or bridge.center != cfg.center_hz:
                bridge.start(fs, cfg.center_hz, vfreqs)
        else:
            bridge.stop()
        for f in vdl2.VDL2_FREQS:
            views.append({"id": f"vdl2-{int(f)}", "freq_hz": f, "label": "VDL2", "kind": "vdl2",
                          "inside": lo <= f <= hi, "level_db": 0.0, "open": False})
        with self._lock:
            self.channels_view = views
            self.vdl2_state.update(enabled=bridge.running, freqs=vfreqs)
        return voices, bursts

    def _decode_acars(self, burst: np.ndarray, freq: float, level: float) -> None:
        for m in acars.Demodulator(AUDIO_RATE).decode(burst):
            self.messages.add({"source": "ACARS", "freq_hz": freq, "level_db": round(level, 1), "hex": "",
                               **m.as_dict()})

    def _add_clip(self, clip: Clip) -> None:
        with self._lock:
            self.clips.append(clip)
        for fn in list(self._clip_listeners):
            try:
                fn(clip)
            except Exception:
                pass

    def _spectrum(self, x: np.ndarray, fs: float, center: float, now: float) -> None:
        n = 2048
        seg = x[: (len(x) // n) * n].reshape(-1, n) * np.hanning(n).astype(np.float32)
        p = np.fft.fftshift((np.abs(np.fft.fft(seg, axis=1)) ** 2).mean(0))
        db = 10 * np.log10(p + 1e-12)
        self.activity.update(db, center, fs, now)
        small = db.reshape(-1, 4).max(1)
        with self._lock:
            self.spectrum = {"center_hz": center, "span_hz": fs, "db": np.round(small - small.max(), 1).tolist()}

    # -- views -----------------------------------------------------------------------------------
    def snapshot(self) -> dict:
        lo, hi = self.window()
        with self._lock:
            return {"state": self.state, "message": self.message, "device": self.device,
                    "config": {k: v for k, v in self.cfg.to_json().items() if k != "channels"},
                    "channels": [dict(v) for v in self.channels_view] or self._static_views(lo, hi),
                    "window": [lo, hi], "vdl2": dict(self.vdl2_state),
                    "activity": self.activity.top(time.time())}

    def _static_views(self, lo: float, hi: float) -> list[dict]:
        out = [{"id": c.id, "freq_hz": c.freq_hz, "label": c.label, "kind": "voice", "inside": lo <= c.freq_hz <= hi,
                "squelch_db": c.squelch_db, "level_db": 0.0, "open": False} for c in self.cfg.channels]
        if self.cfg.acars:
            out += [{"id": f"acars-{int(f)}", "freq_hz": f, "label": "ACARS", "kind": "acars", "inside": lo <= f <= hi,
                     "level_db": 0.0, "open": False} for f in ACARS_FREQS]
        out += [{"id": f"vdl2-{int(f)}", "freq_hz": f, "label": "VDL2", "kind": "vdl2", "inside": lo <= f <= hi,
                 "level_db": 0.0, "open": False} for f in vdl2.VDL2_FREQS]
        return out

    def audio_since(self, seq: int) -> list[tuple[int, dict]]:
        with self._lock:
            return [(s, f) for s, f in self.audio if s > seq]

    def clip(self, clip_id: int) -> Clip | None:
        with self._lock:
            return next((c for c in self.clips if c.id == clip_id), None)

    def clips_list(self) -> list[dict]:
        with self._lock:
            return [c.public() for c in reversed(self.clips)]


_ids = itertools.count(1)


def new_channel_id() -> str:
    return f"ch{int(time.time() * 1000) % 10_000_000}{next(_ids)}"
