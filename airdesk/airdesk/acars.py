"""Plain ACARS (ARINC 618) on VHF: AM carrier, 2400 bit/s MSK audio (1200/2400 Hz tones).

The tones carry the data differentially: the higher tone means "same bit as before", the lower
tone means "the bit flips". Characters are 7-bit ASCII with odd parity, sent LSB first. A frame is
    pre-key (ones) '+' '*' SYN SYN SOH mode address(7) ack label(2) block-id [STX text] ETX|ETB BCS(2) DEL
and the block check is CRC-16/KERMIT over everything from the mode character to the BCS.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
from scipy import signal

BAUD = 2400
SOH, STX, ETX, ETB, SYN, DEL, NAK = 0x01, 0x02, 0x03, 0x17, 0x16, 0x7F, 0x15
_SYNC_BITS = None

LABELS = {
    "_\x7f": "General response", "H1": "Message to/from terminal", "Q0": "Link test", "SA": "Media advisory",
    "5Z": "Airline designated", "10": "Out of gate", "11": "Off ground", "12": "On ground", "13": "In gate",
    "14": "ETA report", "15": "Flight status", "16": "Position report", "20": "Delay report", "80": "Airline position",
    "B6": "ADS-C", "B9": "ATIS request", "AA": "ATS free text", "BA": "ATC communications", "C1": "Uplink to cockpit printer",
    "RA": "Uplink to cockpit printer", "QQ": "Free text", "QF": "Off report", "QG": "Out report", "QH": "On report",
    "QK": "Landing report", "QM": "Arrival information", "QN": "Diversion", "4M": "Cargo information",
}


def _crc16_kermit(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0x8408 if c & 1 else c >> 1
    return c


def _odd_parity(c: int) -> int:
    c &= 0x7F
    return c | (0x80 if bin(c).count("1") % 2 == 0 else 0)


def _parity_ok(b: int) -> bool:
    return bin(b).count("1") % 2 == 1


@dataclass
class Message:
    mode: str
    reg: str
    ack: str
    label: str
    block_id: str
    msg_num: str
    flight: str
    text: str
    corrected: int = 0            # bits fixed by the error correction

    @property
    def label_name(self) -> str:
        return LABELS.get(self.label, "")

    def as_dict(self) -> dict:
        return {"mode": self.mode, "reg": self.reg, "ack": self.ack, "label": self.label.replace("\x7f", "d"),
                "label_name": self.label_name, "block_id": self.block_id, "msg_num": self.msg_num,
                "flight": self.flight, "text": self.text, "corrected": self.corrected}


def _parse(body: bytes) -> Message | None:
    """body = mode .. ETX/ETB, parity stripped."""
    if len(body) < 13:
        return None
    s = body.decode("ascii", "replace")
    mode, addr, ack, label, blk = s[0], s[1:8], s[8], s[9:11], s[11]
    text, msg_num, flight = "", "", ""
    if len(s) > 13 and s[12] == chr(STX):
        text = s[13:-1]
        if blk.isdigit() and len(text) >= 10:              # downlink: message number + flight id first
            msg_num, flight, text = text[:4], text[4:10].strip(), text[10:]
    ack = "" if ack == chr(NAK) else ack
    return Message(mode, addr.strip(".").strip(), ack, label, blk, msg_num, flight, text.rstrip("\r\n"))


def _frames(bits: np.ndarray):
    """Yield (start, data bytes incl. parity) after every SYN SYN SOH in an LSB-first bit stream."""
    n = len(bits) // 8 * 8
    if n < 64:
        return
    weights = 1 << np.arange(8)
    starts = np.flatnonzero(_sync_scan(bits))
    for s in starts:
        k = s + 24
        out = []
        while k + 8 <= len(bits) and len(out) < 260:
            v = int(bits[k:k + 8] @ weights)
            out.append(v)
            k += 8
            if (v & 0x7F) in (ETX, ETB) and len(out) >= 12:
                for _ in range(2):
                    if k + 8 <= len(bits):
                        out.append(int(bits[k:k + 8] @ weights))
                        k += 8
                break
        yield s, out


def _sync_scan(bits: np.ndarray) -> np.ndarray:
    pattern = np.array([(b >> i) & 1 for b in (SYN, SYN, SOH) for i in range(8)], np.int8)
    if len(bits) < 24:
        return np.zeros(0, bool)
    win = np.lib.stride_tricks.sliding_window_view(bits.astype(np.int8), 24)
    return (win == pattern).all(axis=1)


def _check(data: list[int]) -> tuple[bytes, int] | None:
    """Verify the block check, correcting up to two characters that fail parity. Returns (body, bits fixed)."""
    if len(data) < 15 or (data[-3] & 0x7F) not in (ETX, ETB):
        return None
    frame = data[:-2]
    crc = data[-2:]
    if _crc16_kermit(bytes(frame + crc)) == 0 and all(_parity_ok(b) for b in frame):
        return bytes(b & 0x7F for b in frame), 0
    bad = [i for i, b in enumerate(frame) if not _parity_ok(b)]
    if not 0 < len(bad) <= 2:
        return None
    for flips in itertools.product(range(8), repeat=len(bad)):
        trial = list(frame)
        for i, bit in zip(bad, flips):
            trial[i] ^= 1 << bit
        if _crc16_kermit(bytes(trial + crc)) == 0:
            return bytes(b & 0x7F for b in trial), len(bad)
    return None


class Demodulator:
    """Turns one AM burst (complex baseband) into ACARS messages."""

    def __init__(self, sample_rate: float):
        self.fs = float(sample_rate)
        self.up = max(1, int(round(10 * BAUD / self.fs)))          # resample to about 10 samples per bit
        self.fa = self.fs * self.up
        self.sps = self.fa / BAUD
        self.lp = signal.firwin(41, 1500, fs=self.fa)

    def _dphi(self, iq: np.ndarray) -> np.ndarray:
        audio = np.abs(iq).astype(np.float64)
        audio -= audio.mean()
        if self.up > 1:
            audio = signal.resample_poly(audio, self.up, 1)
        t = np.arange(len(audio)) / self.fa
        z = signal.lfilter(self.lp, 1, audio * np.exp(-2j * np.pi * 1800 * t))
        lag = int(round(self.sps))
        return np.angle(z[lag:] * np.conj(z[:-lag]))

    def tone_candidates(self, iq: np.ndarray, max_tries: int = 400):
        """Tone sequences for plausible symbol timings, most likely first.

        Aircraft bit clocks differ from ours by up to ~0.05 %, enough to slip a bit over a long frame,
        and the timing information in AM-detected MSK is weak. So timing (phase and drift) is
        searched, ordered by a cheap quality score; the caller stops at the first frame that passes
        parity and the CRC, which makes a wrong timing guess harmless.
        """
        dphi = self._dphi(iq)
        nb = int(len(dphi) // self.sps) - 2
        if nb < 40:
            return
        k = np.arange(nb)[:, None]
        p = np.arange(int(self.sps))[None, :]
        rot = np.exp(2j * np.pi * np.arange(int(self.sps)) / self.sps)
        idx = np.arange(len(dphi))
        drifts = []
        for drift in np.arange(-0.008, 0.00801, 0.0005):
            mag = np.abs(np.interp((k * (self.sps + drift) + p).ravel(), idx, dphi)).reshape(nb, -1)
            w = (mag @ rot).sum()
            drifts.append((abs(w) / (mag.sum() + 1e-12), drift, float(np.angle(w) * self.sps / (2 * np.pi))))
        drifts.sort(reverse=True)
        steps = np.arange(0, self.sps, 0.5)
        order = []
        for di, (_, drift, est) in enumerate(drifts):
            dist = np.abs((steps - est + self.sps / 2) % self.sps - self.sps / 2)
            for pi, ph in enumerate(steps[np.argsort(dist)]):
                order.append((di + pi, drift, ph))
        order.sort(key=lambda o: o[0])
        for _, drift, ph in order[:max_tries]:
            pos = np.arange(nb) * (self.sps + drift) + ph
            pos = pos[pos < len(dphi) - 1]
            yield (np.interp(pos, idx, dphi) > 0).astype(np.int8)

    def decode(self, iq: np.ndarray) -> list[Message]:
        out, seen = [], set()
        for tone in self.tone_candidates(iq):
            raw = (np.cumsum(1 - tone) % 2).astype(np.int8)          # bit flips on the low tone
            for bits in (raw, 1 - raw):
                for _, data in _frames(bits):
                    checked = _check(data)
                    if checked is None:
                        continue
                    body, fixed = checked
                    if body in seen:
                        continue
                    seen.add(body)
                    msg = _parse(body)
                    if msg:
                        msg.corrected = fixed
                        out.append(msg)
            if out:
                break
        return out


# -- test signal generator --------------------------------------------------------------------------
def encode(mode: str, reg: str, label: str, block_id: str, text: str = "", ack: str = chr(NAK),
           msg_num: str = "", flight: str = "") -> bytes:
    """Frame bytes (with parity) from SOH to DEL."""
    addr = reg.rjust(7, ".")[:7]
    body = mode + addr + ack + label + block_id
    if text or msg_num or flight:
        body += chr(STX) + (msg_num.ljust(4)[:4] + flight.ljust(6)[:6] if msg_num or flight else "") + text
    body += chr(ETX)
    framed = [_odd_parity(ord(c)) for c in body]
    crc = _crc16_kermit(bytes(framed))
    return bytes([_odd_parity(SOH)] + framed + [crc & 0xFF, crc >> 8, _odd_parity(DEL)])


def modulate(frame: bytes, fs: float, preamble_bits: int = 120, depth: float = 0.6) -> np.ndarray:
    """AM-modulated MSK burst as complex baseband at fs (carrier at 0 Hz)."""
    pre = [_odd_parity(ord("+")), _odd_parity(ord("*")), SYN, SYN]
    data = [1] * preamble_bits + [(b >> i) & 1 for b in pre + list(frame) for i in range(8)] + [1] * 16
    tones, prev = [], 1
    for bit in data:
        tones.append(1 if bit == prev else 0)
        prev = bit
    spb = fs / BAUD
    n = int(len(tones) * spb)
    idx = np.minimum((np.arange(n) / spb).astype(int), len(tones) - 1)
    freq = np.where(np.array(tones)[idx] == 1, 2400.0, 1200.0)
    audio = np.sin(2 * np.pi * np.cumsum(freq) / fs)
    return ((1 + depth * audio) * 0.5).astype(np.complex64)
