"""Channelizer and AM voice channels for the airband."""
from __future__ import annotations

import collections
import io
import time
import wave
from dataclasses import dataclass, field

import numpy as np
from scipy import signal

AUDIO_RATE = 12_000


class Channelizer:
    """Cuts narrow channels out of a wideband IQ stream with one FFT per block (overlap-save).

    Each block of N input samples advances by L = N/2. A channel takes M = N/D bins around its
    frequency, weighted by a smooth low-pass, and an M-point inverse FFT gives L/D new samples
    at fs/D. With fs = 2.4 MHz, N = 8000 and D = 200 that is 12 kHz audio-rate channels, at the
    cost of one 8000-point FFT per 1.7 ms of signal however many channels there are.
    """

    def __init__(self, fs: float, out_rate: float = AUDIO_RATE, n_fft: int = 8000, bw: float = 9_000):
        self.fs = float(fs)
        self.D = int(round(fs / out_rate))
        if abs(fs / self.D - out_rate) > 1 or n_fft % (2 * self.D):
            raise ValueError("sample rate, output rate and FFT size do not fit together")
        self.N, self.L = n_fft, n_fft // 2
        self.M = n_fft // self.D
        self.out_rate = fs / self.D
        f = np.fft.fftfreq(self.M, 1 / self.out_rate)
        # flat to +-bw/2, raised-cosine roll-off to the edge of the output band
        edge = self.out_rate / 2
        h = np.clip((edge - np.abs(f)) / (edge - bw / 2), 0, 1)
        self.H = (0.5 - 0.5 * np.cos(np.pi * h)).astype(np.float32)
        self._buf = np.zeros(self.N - self.L, np.complex64)
        self.channels: dict[str, float] = {}               # id -> offset from centre in Hz
        self._bins: dict[str, np.ndarray] = {}

    def set_channels(self, offsets: dict[str, float]) -> None:
        self.channels = dict(offsets)
        self._bins = {}
        for cid, off in self.channels.items():
            k0 = int(round(off / (self.fs / self.N)))
            idx = (k0 + np.fft.fftfreq(self.M, 1 / self.M).astype(int)) % self.N
            self._bins[cid] = idx

    def process(self, x: np.ndarray) -> dict[str, np.ndarray]:
        """Feed any number of samples; returns new output samples per channel."""
        data = np.concatenate([self._buf, x.astype(np.complex64)])
        nblk = (len(data) - (self.N - self.L)) // self.L
        out = {cid: [] for cid in self.channels}
        keep = self.L // self.D
        for b in range(nblk):
            X = np.fft.fft(data[b * self.L: b * self.L + self.N])
            for cid, idx in self._bins.items():
                y = np.fft.ifft(X[idx] * self.H) * (self.M / self.N)
                out[cid].append(y[-keep:])
        self._buf = data[nblk * self.L:]
        return {cid: (np.concatenate(v).astype(np.complex64) if v else np.zeros(0, np.complex64)) for cid, v in out.items()}


@dataclass
class Clip:
    id: int
    channel: str
    freq_hz: float
    start: float
    level_db: float
    audio: np.ndarray = field(repr=False)

    @property
    def duration(self) -> float:
        return len(self.audio) / AUDIO_RATE

    def public(self) -> dict:
        return {"id": self.id, "channel": self.channel, "freq_hz": self.freq_hz, "start": self.start,
                "duration": round(self.duration, 2), "level_db": round(self.level_db, 1)}

    def wav(self) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(AUDIO_RATE)
            w.writeframes(self.audio.astype("<i2").tobytes())
        return buf.getvalue()


class VoiceChannel:
    """AM airband voice: envelope detector, automatic level, squelch that tracks the noise floor."""

    HANG_S = 0.6
    MAX_CLIP_S = 60.0

    def __init__(self, cid: str, freq_hz: float, squelch_db: float = 8.0, rate: float = AUDIO_RATE):
        self.id, self.freq_hz, self.squelch_db, self.rate = cid, freq_hz, squelch_db, rate
        self.floor: float | None = None
        self.level_db = -120.0
        self.open = False
        self._hang = 0.0
        self._agc = 1e-3
        self._clip: list[np.ndarray] = []
        self._clip_start = 0.0
        self._clip_peak = -120.0
        self._hp = signal.butter(2, [250, 3200], btype="bandpass", fs=rate, output="sos")
        self._zi = signal.sosfilt_zi(self._hp) * 0.0

    def process(self, iq: np.ndarray, now: float) -> tuple[np.ndarray, Clip | None]:
        """Returns (int16 audio, silent while squelched; finished clip or None)."""
        if len(iq) == 0:
            return np.zeros(0, np.int16), None
        env = np.abs(iq)
        p = float(np.mean(env ** 2)) + 1e-20
        self.level_db = 10 * np.log10(p)
        if self.floor is None:
            self.floor = self.level_db
        if not self.open:                                     # follow the noise floor while closed
            a = 0.02 if self.level_db > self.floor else 0.2
            self.floor += a * (self.level_db - self.floor)
        dt = len(iq) / self.rate
        if self.level_db > self.floor + self.squelch_db:
            self.open, self._hang = True, self.HANG_S
        elif self.open:
            self._hang -= dt
            if self._hang <= 0:
                self.open = False
        carrier = float(np.mean(env))
        self._agc += 0.2 * (carrier - self._agc)
        audio, self._zi = signal.sosfilt(self._hp, env / max(self._agc, 1e-9) - 1.0, zi=self._zi)
        pcm = np.clip(audio * 14000, -32767, 32767).astype(np.int16) if self.open else np.zeros(len(iq), np.int16)
        finished = None
        if self.open:
            if not self._clip:
                self._clip_start, self._clip_peak, self._clip_n, self._active_n = now - dt, self.level_db, 0, 0
            self._clip.append(pcm)
            self._clip_n += len(pcm)
            if self.level_db > self.floor + self.squelch_db:
                self._active_n = self._clip_n                     # end of the carrier so far
            self._clip_peak = max(self._clip_peak, self.level_db)
            if sum(len(c) for c in self._clip) / self.rate >= self.MAX_CLIP_S:
                finished = self._finish()
        elif self._clip:
            finished = self._finish()
        return pcm, finished

    _ids = iter(range(1, 1 << 62))

    def _finish(self) -> Clip | None:
        audio = np.concatenate(self._clip)
        self._clip = []
        if self._active_n < 0.35 * self.rate:                 # clicks and noise blips are not transmissions
            return None
        return Clip(next(VoiceChannel._ids), self.id, self.freq_hz, self._clip_start,
                    self._clip_peak - (self.floor or 0), audio[: self._active_n + int(0.1 * self.rate)])


class BurstCatcher:
    """Collects a data burst (ACARS) on a channel: from the carrier appearing to it ending."""

    def __init__(self, rate: float = AUDIO_RATE, open_db: float = 8.0, max_s: float = 3.0):
        self.rate, self.open_db, self.max_n = rate, open_db, int(max_s * rate)
        self.floor: float | None = None
        self.level_db = -120.0
        self._buf: list[np.ndarray] = []
        self._pre = collections.deque(maxlen=3)
        self._quiet = 0

    def process(self, iq: np.ndarray) -> np.ndarray | None:
        if len(iq) == 0:
            return None
        lvl = 10 * np.log10(float(np.mean(np.abs(iq) ** 2)) + 1e-20)
        self.level_db = lvl
        if self.floor is None:
            self.floor = lvl
        active = lvl > self.floor + self.open_db
        if not self._buf and not active:
            self.floor += (0.02 if lvl > self.floor else 0.2) * (lvl - self.floor)
            self._pre.append(iq)
            return None
        if active:
            if not self._buf:
                self._buf = list(self._pre)
            self._quiet = 0
        else:
            self._quiet += 1
        self._buf.append(iq)
        if self._quiet >= 2 or sum(len(b) for b in self._buf) >= self.max_n:
            burst = np.concatenate(self._buf)
            self._buf, self._quiet = [], 0
            self._pre.clear()
            return burst
        return None
