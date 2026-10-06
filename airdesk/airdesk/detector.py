"""Finding voice channels: which 8.33 kHz airband channels carry AM speech right now.

Every chunk (50 ms) the power of each channel on the 8.33 kHz raster in the window is measured
from one batch of FFTs. A channel is active while it stands 8 dB over its own noise floor (5 dB to
stay active). When activity ends, the burst is voice if it lasted at least 0.7 s and its level
moved the way speech does (standard deviation of the 50 ms levels >= 0.3 dB); data bursts (ACARS, VDL2) are far shorter, and plain carriers (spurs,
birdies) do not move at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

AIRBAND = (118.0e6, 137.0e6)
RASTER = 25e3 / 3                         # 8.33 kHz channel spacing (25 kHz channels sit on it too)
DATA_FREQS = (131.525e6, 131.725e6, 131.825e6, 131.850e6)
DATA_BANDS = ((136.700e6, 137.0e6),)      # VDL2


def is_data(freq: float) -> bool:
    return any(abs(freq - f) < 12e3 for f in DATA_FREQS) or any(lo <= freq <= hi for lo, hi in DATA_BANDS)


def snap(freq: float) -> float:
    """Nearest 8.33 kHz raster frequency."""
    return AIRBAND[0] + round((freq - AIRBAND[0]) / RASTER) * RASTER


@dataclass
class Burst:
    freq: float
    start: float
    snr_db: float
    levels: list = field(default_factory=list)
    end: float = 0.0
    cut: bool = False                          # ended because the radio moved, not because it stopped

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def modulated(self) -> bool:
        lv = np.array(self.levels[1:-1] or self.levels)
        # AM speech moves the channel power by ~1-1.5 dB between syllables and pauses; a plain carrier
        # (spur, birdie) moves by a few hundredths of a dB even when weak
        return len(lv) >= 3 and float(np.std(lv)) >= 0.3

    @property
    def is_voice(self) -> bool:
        return self.duration >= 0.7 and self.modulated


class VoiceDetector:
    N = 4096
    OPEN_DB, HOLD_DB = 8.0, 5.0
    GAP_S = 0.3

    def __init__(self, fs: float):
        self.fs = fs
        self.floors: dict[float, float] = {}                 # per raster frequency, kept across windows
        self.active: dict[float, Burst] = {}
        self._quiet: dict[float, float] = {}
        self.center = 0.0
        self._freqs = np.zeros(0)
        self._bins: list[slice] = []
        self._skip = 0

    def set_window(self, center: float, half_width: float, now: float = 0.0) -> list[Burst]:
        """New window: bursts in progress are cut off and returned (ended now)."""
        self.center = center
        lo, hi = max(center - half_width, AIRBAND[0]), min(center + half_width, AIRBAND[1])
        k0, k1 = int(np.ceil((lo - AIRBAND[0]) / RASTER)), int(np.floor((hi - AIRBAND[0]) / RASTER))
        freqs = AIRBAND[0] + np.arange(k0, k1 + 1) * RASTER
        freqs = freqs[np.abs(freqs - center) > 12e3]          # the receiver's own spike at the centre
        self._freqs = np.array([f for f in freqs if not is_data(f)])
        binw = self.fs / self.N
        idx = lambda f: int(round((f - center) / binw)) + self.N // 2
        half = max(1, int(round(3.2e3 / binw)))
        self._bins = [slice(idx(f) - half, idx(f) + half + 1) for f in self._freqs]
        self._skip = 2                                        # first chunks after a retune are unreliable
        cut = list(self.active.values())
        for b in cut:
            b.end, b.cut = now, True
        self.active.clear()
        self._quiet.clear()
        return cut

    def channel_levels(self, x: np.ndarray) -> np.ndarray:
        n = len(x) // self.N
        if n == 0 or not len(self._freqs):
            return np.zeros(len(self._freqs))
        win = self._win if len(getattr(self, "_win", ())) == self.N else np.hanning(self.N).astype(np.float32)
        self._win = win                                       # low leakage: strong stations stay in their channel
        spec = np.fft.fftshift(np.abs(np.fft.fft(x[: n * self.N].reshape(n, self.N) * win, axis=1)) ** 2, axes=1).mean(0)
        return 10 * np.log10(np.array([spec[s].sum() for s in self._bins]) + 1e-20)

    def process(self, x: np.ndarray, now: float, dt: float) -> tuple[list[Burst], list[Burst]]:
        """Returns (bursts that started, bursts that ended) in this chunk."""
        if self._skip:
            self._skip -= 1
            return [], []
        lv = self.channel_levels(x)
        started, ended = [], []
        floors = np.array([self.floors.get(f, np.nan) for f in self._freqs])
        new = np.isnan(floors)
        floors[new] = lv[new]
        snr = lv - floors
        # a strong channel splatters into its neighbours; only count local maxima
        louder = np.zeros(len(lv), bool)
        for d in (1, 2):
            louder[d:] |= lv[:-d] > lv[d:] + 6
            louder[:-d] |= lv[d:] > lv[:-d] + 6
        for i, f in enumerate(self._freqs):
            b = self.active.get(f)
            if b is None:
                if snr[i] > self.OPEN_DB and not louder[i] and not new[i]:
                    b = self.active[f] = Burst(f, now - dt, float(snr[i]))
                    started.append(b)
                else:                                     # follow the floor: quickly down, slowly up
                    floors[i] += (0.2 if snr[i] < 0 else 0.02) * (lv[i] - floors[i])
            if b is not None:
                if snr[i] > self.HOLD_DB:
                    b.levels.append(float(lv[i]))
                    b.snr_db = max(b.snr_db, float(snr[i]))
                    self._quiet[f] = 0.0
                    floors[i] += 0.0005 * (lv[i] - floors[i])  # a carrier that never stops becomes the floor
                else:
                    self._quiet[f] = self._quiet.get(f, 0.0) + dt
                    if self._quiet[f] >= self.GAP_S:
                        b.end = now - self._quiet[f]
                        ended.append(b)
                        del self.active[f]
                        self._quiet.pop(f, None)
        for i, f in enumerate(self._freqs):
            self.floors[f] = float(floors[i])
        return started, ended
