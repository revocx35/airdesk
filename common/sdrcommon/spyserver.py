"""Client for Airspy SPY Server (protocol 2.0.1700), streaming unsigned 8-bit IQ."""
from __future__ import annotations

import socket
import struct
import time

import numpy as np

PROTOCOL = (2 << 24) | (0 << 16) | 1700
CMD_HELLO, CMD_GET, CMD_SET, CMD_PING = 0, 1, 2, 3
SET_MODE, SET_ENABLED, SET_GAIN = 0, 1, 2
SET_IQ_FORMAT, SET_IQ_FREQ, SET_IQ_DECIMATION = 100, 101, 102
SET_FFT_FREQ = 201
MSG_DEVICE_INFO, MSG_CLIENT_SYNC, MSG_UINT8_IQ = 0, 1, 100
STREAM_IQ, FORMAT_UINT8 = 1, 1
DEVICES = {1: "Airspy One", 2: "Airspy HF+", 3: "RTL-SDR"}


class SourceError(Exception):
    """A problem the user can act on: wrong address, radio busy or unplugged."""


_LUT = ((np.arange(256, dtype=np.float32) - 127.5) / 127.5).astype(np.float32)


def u8_to_complex(buf) -> np.ndarray:
    a = _LUT[np.frombuffer(buf, np.uint8)]
    return a[: len(a) & ~1].view(np.complex64)


class SpyServerSource:
    def __init__(self, host: str, port: int = 5555, client_name: str = "sdr-app", timeout: float = 5.0):
        self.host, self.port, self.client_name, self.timeout = host, int(port), client_name, timeout
        self.sock: socket.socket | None = None
        self.device: dict | None = None
        self.sync: dict | None = None
        self.sample_rate = 0.0
        self.center_hz = 0.0
        self.gain_steps = 0
        self.description = ""
        self._gain: int | None = None
        self._pending: list[np.ndarray] = []
        self._pending_n = 0
        self._handshake = False

    # -- wire --------------------------------------------------------------------------------
    def _recv(self, n: int) -> bytearray:
        buf = bytearray(n)
        view, got = memoryview(buf), 0
        while got < n:
            try:
                k = self.sock.recv_into(view[got:], n - got)
            except socket.timeout as e:
                raise SourceError(f"SpyServer {self.host}:{self.port} stopped sending data") from e
            except OSError as e:
                raise SourceError(f"Lost the connection to SpyServer {self.host}:{self.port} ({e})") from e
            if k == 0:
                if self._handshake:
                    raise SourceError(f"SpyServer {self.host}:{self.port} refused this app. Another app may be "
                                      "using it, or this app is switched off in SpySwitch")
                raise SourceError(f"SpyServer {self.host}:{self.port} closed the connection")
            got += k
        return buf

    def _send(self, cmd: int, body: bytes) -> None:
        try:
            self.sock.sendall(struct.pack("<II", cmd, len(body)) + body)
        except OSError as e:
            raise SourceError(f"Lost the connection to SpyServer {self.host}:{self.port} ({e})") from e

    def _set(self, setting: int, value: int) -> None:
        self._send(CMD_SET, struct.pack("<II", setting, value))

    def _message(self):
        _proto, mtype, _stream, _seq, size = struct.unpack("<5I", self._recv(20))
        if size > 1 << 22:
            raise SourceError("SpyServer sent a malformed message")
        body = self._recv(size)
        t = mtype & 0xFFFF
        if t == MSG_DEVICE_INFO:
            self.device = dict(zip(("type", "serial", "max_rate", "max_bw", "decimation_stages", "gain_stages",
                                    "max_gain_index", "min_freq", "max_freq", "resolution", "min_iq_decimation",
                                    "forced_iq_format"), struct.unpack("<12I", body[:48])))
        elif t == MSG_CLIENT_SYNC:
            self.sync = dict(zip(("can_control", "gain", "device_center", "iq_center", "fft_center",
                                  "min_iq_center", "max_iq_center", "min_fft_center", "max_fft_center"),
                                 struct.unpack("<9I", body[:36])))
        return t, body

    # -- public ------------------------------------------------------------------------------
    def open(self) -> None:
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise SourceError(f"Cannot connect to SpyServer {self.host}:{self.port} ({e.strerror or e})") from e
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._handshake = True
        self._send(CMD_HELLO, struct.pack("<I", PROTOCOL) + self.client_name.encode()[:64])
        deadline = time.monotonic() + self.timeout
        while self.device is None or self.sync is None:
            if time.monotonic() > deadline:
                raise SourceError("SpyServer did not answer the handshake")
            self._message()
        self._handshake = False
        if self.device["type"] == 0:
            raise SourceError("SpyServer has no radio attached (is the dongle plugged in?)")
        if not self.sync["can_control"]:
            raise SourceError("SpyServer does not let this app tune. Another client may be connected, "
                              "or allow_control is off in spyserver.config")
        decim = self.device["min_iq_decimation"]
        self.sample_rate = self.device["max_rate"] / (1 << decim)
        self.gain_steps = self.device["max_gain_index"] + 1
        self.description = f"{DEVICES.get(self.device['type'], 'SDR')} via SpyServer {self.host}:{self.port}"
        self._set(SET_IQ_FORMAT, FORMAT_UINT8)
        self._set(SET_MODE, STREAM_IQ)
        self._set(SET_IQ_DECIMATION, decim)

    def tune(self, hz: float) -> None:
        lo, hi = self.sync["min_iq_center"], self.sync["max_iq_center"]
        if not lo <= hz <= hi:
            raise SourceError(f"{hz / 1e6:.3f} MHz is outside the radio's range ({lo / 1e6:.0f}-{hi / 1e6:.0f} MHz)")
        self._set(SET_IQ_FREQ, int(round(hz)))
        self.center_hz = hz

    def set_gain(self, step: int) -> None:
        self._gain = int(max(0, min(step, self.gain_steps - 1)))
        self._set(SET_GAIN, self._gain)

    def start(self) -> None:
        self._set(SET_ENABLED, 1)
        # SpyServer wakes the radio at its configured initial gain when streaming starts, yet still
        # believes our earlier setting is in force and ignores the same value again. Step away and back.
        if self._gain is not None:
            self._set(SET_GAIN, self._gain - 1 if self._gain > 0 else self._gain + 1)
            self._set(SET_GAIN, self._gain)
        if self.center_hz:
            self._set(SET_IQ_FREQ, int(round(self.center_hz)))

    def read(self, n: int) -> np.ndarray:
        """Exactly n samples, blocking until they arrive."""
        while self._pending_n < n:
            while True:
                t, body = self._message()
                if t == MSG_UINT8_IQ and body:
                    b = u8_to_complex(body)
                    break
            self._pending.append(b)
            self._pending_n += len(b)
        data = np.concatenate(self._pending) if len(self._pending) > 1 else self._pending[0]
        out, rest = data[:n], data[n:]
        self._pending = [rest] if len(rest) else []
        self._pending_n = len(rest)
        return out

    def discard(self, seconds: float) -> None:
        self._pending, self._pending_n = [], 0
        self.read(max(1, int(seconds * self.sample_rate)))

    def close(self) -> None:
        if self.sock is not None:
            try:
                self._set(SET_ENABLED, 0)
            except SourceError:
                pass
            try:
                self.sock.close()
            finally:
                self.sock = None
