"""A minimal threaded SpyServer: handshake, settings, and 8-bit IQ while streaming is on."""
from __future__ import annotations

import select
import socket
import struct
import threading
import time

import numpy as np

FS = 2_400_000


class FakeSpyServer:
    def __init__(self, signal=None, max_clients: int = 4, pace: bool = True):
        """signal(center_hz, n) -> complex64 samples; default is low-level noise."""
        self.signal = signal or (lambda center, n: (np.random.default_rng().normal(0, 0.05, 2 * n)).astype(np.float32).view(np.complex64))
        self.max_clients, self.pace = max_clients, pace
        self.lsock = socket.socket()
        self.lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(8)
        self.port = self.lsock.getsockname()[1]
        self.hellos: list[str] = []
        self.settings: list[dict] = []
        self.clients = 0
        self._stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._stop.is_set():
            r, _, _ = select.select([self.lsock], [], [], 0.1)
            if r:
                try:
                    conn, _ = self.lsock.accept()
                except OSError:
                    return
                threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def close(self):
        self._stop.set()
        self.lsock.close()

    @staticmethod
    def _msg(conn, mtype, body):
        conn.sendall(struct.pack("<5I", 0x02000000 | 1700, mtype, 0, 0, len(body)) + body)

    def _client(self, conn):
        self.clients += 1
        settings = {}
        self.settings.append(settings)
        center = 100_000_000
        try:
            _cmd, size = struct.unpack("<II", conn.recv(8))
            body = b""
            while len(body) < size:
                body += conn.recv(size - len(body))
            self.hellos.append(body[4:].decode(errors="replace"))
            if self.clients > self.max_clients:
                return
            self._msg(conn, 0, struct.pack("<12I", 3, 0, FS, 2_000_000, 9, 0, 29, 24_000_000, 1_800_000_000, 8, 0, 0))
            self._msg(conn, 1, struct.pack("<9I", 1, 5, center, center, center, 25_000_000, 1_799_000_000,
                                           25_000_000, 1_799_000_000))
            buf, t0, sent = b"", None, 0
            while not self._stop.is_set():
                r, _, _ = select.select([conn], [], [], 0)
                if r:
                    data = conn.recv(4096)
                    if not data:
                        return
                    buf += data
                    while len(buf) >= 8:
                        cmd, size = struct.unpack("<II", buf[:8])
                        if len(buf) < 8 + size:
                            break
                        body, buf = buf[8:8 + size], buf[8 + size:]
                        if cmd == 2:
                            k, v = struct.unpack("<II", body[:8])
                            settings[k] = v
                            if k == 101:
                                center = v
                if settings.get(1) == 1:
                    x = self.signal(center, 16384)
                    u8 = np.clip(np.round(x.view(np.float32) * 100 + 127.5), 0, 255).astype(np.uint8)
                    self._msg(conn, 100, u8.tobytes())
                    if self.pace:
                        t0 = t0 or time.monotonic()
                        sent += 16384
                        ahead = sent / FS - (time.monotonic() - t0)
                        if ahead > 0:
                            time.sleep(ahead)
                else:
                    time.sleep(0.01)
        except OSError:
            pass
        finally:
            self.clients -= 1
            conn.close()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p
