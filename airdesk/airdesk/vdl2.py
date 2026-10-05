"""VDL Mode 2 via dumpvdl2: we hand it IQ around the VDL2 channels, it hands back JSON messages."""
from __future__ import annotations

import json
import logging
import queue
import shutil
import subprocess
import threading
from fractions import Fraction

import numpy as np
from scipy import signal

log = logging.getLogger("airdesk.vdl2")
VDL2_FREQS = (136.725e6, 136.775e6, 136.875e6, 136.975e6)
VDL2_RATE = 1_050_000                       # dumpvdl2 with --oversample 10


class StreamResampler:
    """Rational resampler (up/down polyphase FIR) that keeps state between calls.

    Resampling each chunk on its own would put filter edge effects at every chunk boundary. This
    gives exactly the samples of one upfirdn(h, x, up, down) call over the whole stream:
        y[m] = sum_j h[m*down - j*up] * x[j]
    """

    def __init__(self, up: int, down: int, taps_per_phase: int = 24):
        self.up, self.down, self.T = up, down, taps_per_phase
        h = signal.firwin(taps_per_phase * up, 1.0 / max(up, down), window=("kaiser", 6.0)) * up
        self.h = h.astype(np.float32)
        self.poly = self.h.reshape(taps_per_phase, up).T.copy()      # poly[phase, i] = h[phase + i*up]
        self.buf = np.zeros(taps_per_phase - 1, np.complex64)         # zero history before the stream
        self.b0 = -(taps_per_phase - 1)                               # global index of buf[0]
        self.next_out = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self.buf, x.astype(np.complex64)])
        last = self.b0 + len(buf) - 1
        m_hi = (last * self.up) // self.down
        out = np.zeros(0, np.complex64)
        if m_hi >= self.next_out:
            m = np.arange(self.next_out, m_hi + 1)
            j_hi = (m * self.down) // self.up
            phase = m * self.down - j_hi * self.up
            local = (j_hi - self.b0)[:, None] - np.arange(self.T)[None, :]
            out = np.einsum("ij,ij->i", buf[local], self.poly[phase]).astype(np.complex64)
            self.next_out = m_hi + 1
        keep = (self.next_out * self.down) // self.up - self.T + 1 - self.b0
        keep = max(0, min(keep, len(buf)))
        self.buf, self.b0 = buf[keep:], self.b0 + keep
        return out


def available(binary: str = "dumpvdl2") -> bool:
    return shutil.which(binary) is not None


def normalize(raw: dict) -> dict | None:
    """dumpvdl2 JSON -> the message shape the app uses, or None.

    Only frames that carry ACARS content are kept; the rest of VDL2 traffic (link setup, X.25 and
    CLNP keepalives, acknowledgements) is network housekeeping with nothing to read.
    """
    v = raw.get("vdl2") or {}
    av = v.get("avlc") or {}
    src = av.get("src") or {}
    acars = av.get("acars")
    if not acars:
        return None
    from .acars import LABELS
    label = acars.get("label", "")
    return {"source": "VDL2", "t": (v.get("t") or {}).get("sec"), "freq_hz": v.get("freq"),
            "level_db": round(float(v.get("sig_level", 0)) - float(v.get("noise_level", 0)), 1),
            "hex": (src.get("addr") or "").upper() if src.get("type") == "Aircraft" else "",
            "reg": acars.get("reg", "").strip(".").strip(), "flight": acars.get("flight", "").strip(),
            "label": label.replace("\x7f", "d"), "label_name": LABELS.get(label, ""),
            "text": acars.get("msg_text", "").strip(), "msg_num": acars.get("msg_num", ""),
            "uplink": src.get("type") != "Aircraft"}


class Vdl2Bridge:
    """Feeds IQ to a dumpvdl2 process and reports decoded messages through on_message."""

    def __init__(self, on_message, binary: str = "dumpvdl2"):
        self.on_message, self.binary = on_message, binary
        self.proc: subprocess.Popen | None = None
        self.freqs: list[float] = []
        self.center = 0.0
        self.group = 0.0
        self.dropped = 0
        self.decoded = 0
        self._q: queue.Queue = queue.Queue(maxsize=40)
        self._phase = 0.0
        self._resampler: StreamResampler | None = None
        self._step = 0.0

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, fs: float, center: float, freqs: list[float]) -> None:
        self.stop()
        self.freqs = sorted(freqs)
        self.center = center
        self.group = (self.freqs[0] + self.freqs[-1]) / 2
        r = Fraction(VDL2_RATE / fs).limit_denominator(1000)
        self._resampler = StreamResampler(r.numerator, r.denominator)
        self._step = -2 * np.pi * (self.group - center) / fs
        self._phase = 0.0
        cmd = [self.binary, "--iq-file", "-", "--sample-format", "S16_LE", "--oversample", "10",
               "--centerfreq", str(int(self.group)), "--output", "decoded:json:file:path=-",
               *[str(int(f)) for f in self.freqs]]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     bufsize=0)
        threading.Thread(target=self._writer, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        log.info("dumpvdl2 started on %s", ", ".join(f"{f / 1e6:.3f}" for f in self.freqs))

    def feed(self, x: np.ndarray) -> None:
        if not self.running:
            return
        n = np.arange(len(x))
        y = x * np.exp(1j * (self._phase + self._step * n)).astype(np.complex64)
        self._phase = float((self._phase + self._step * len(x)) % (2 * np.pi))
        z = self._resampler.process(y)
        s16 = np.empty(2 * len(z), np.int16)
        s16[0::2] = np.clip(z.real * 16000, -32767, 32767)
        s16[1::2] = np.clip(z.imag * 16000, -32767, 32767)
        try:
            self._q.put_nowait(s16.tobytes())
        except queue.Full:
            self.dropped += 1

    def _writer(self, proc: subprocess.Popen) -> None:
        while proc.poll() is None:
            try:
                data = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                proc.stdin.write(data)
            except (BrokenPipeError, OSError, ValueError):
                return

    def _reader(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            try:
                msg = normalize(json.loads(line))
            except (ValueError, TypeError):
                continue
            if msg:
                self.decoded += 1
                self.on_message(msg)

    def stop(self) -> None:
        if self.proc is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()
            self.proc = None
        while not self._q.empty():
            self._q.get_nowait()
