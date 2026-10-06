import time

import numpy as np
import pytest

from fake_spyserver import FS, FakeSpyServer
from airdesk import scanner
from airdesk.detector import RASTER, VoiceDetector, is_data, snap
from airdesk.messages import MessageLog
from airdesk.radio import ConfigStore, RadioConfig, RadioEngine
from airdesk.scanner import Scheduler, make_segments, smooth
from airdesk.store import DAY, Store, mulaw_decode, mulaw_encode
from test_app import Scene, speech_am, wait_for

CHUNK = int(FS * 0.05)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# -- detector ----------------------------------------------------------------------------------------
def run_detector(signals, seconds=3.0, center=124.0e6):
    """signals: [(freq, complex baseband at FS, start_s)] -> (started, ended) bursts."""
    det = VoiceDetector(FS)
    det.set_window(center, 0.42 * FS)
    rng = np.random.default_rng(5)
    n = int(seconds * FS)
    started, ended = [], []
    for i in range(0, n, CHUNK):
        k = np.arange(i, i + CHUNK)
        x = (rng.normal(0, 0.004, 2 * CHUNK)).astype(np.float32).view(np.complex64)
        for f, sig, start in signals:
            idx = k - int(start * FS)
            ok = (idx >= 0) & (idx < len(sig))
            if ok.any():
                x[ok] += sig[idx[ok]] * np.exp(2j * np.pi * (f - center) * k[ok] / FS).astype(np.complex64)
        s, e = det.process(x, i / FS, 0.05)
        started += s
        ended += e
    return started, ended


def test_detects_speech_on_its_raster_channel():
    f = snap(124.3)                       # not on the raster: snapping puts it on the nearest channel
    f = snap(124.350e6)
    started, ended = run_detector([(f + 150, speech_am(1.5, FS), 0.5)])
    assert len(started) == 1 and abs(started[0].freq - f) < 1
    assert len(ended) == 1 and ended[0].is_voice and 1.3 < ended[0].duration < 1.8


def test_data_bursts_and_plain_carriers_are_not_voice():
    short = speech_am(0.3, FS)                                     # a data-burst length
    plain = np.full(int(1.5 * FS), 0.15, np.complex64)             # unmodulated carrier
    started, ended = run_detector([(snap(123.5e6), short, 0.5), (snap(124.6e6), plain, 0.5)])
    verdicts = {round(b.freq / 1e3): b.is_voice for b in ended}
    assert verdicts == {round(snap(123.5e6) / 1e3): False, round(snap(124.6e6) / 1e3): False}


def test_strong_station_does_not_light_up_its_neighbours():
    f = snap(124.0e6 + 300e3)
    started, _ = run_detector([(f, speech_am(1.0, FS, carrier=0.8), 0.5)])
    assert [round(b.freq) for b in started] == [round(f)]


def test_data_frequencies_and_window_edges():
    assert is_data(131.525e6) and is_data(136.975e6) and not is_data(121.5e6)
    assert abs(snap(118.0083e6) - (118e6 + RASTER)) < 1
    det = VoiceDetector(FS)
    det.set_window(136.4e6, 0.42 * FS)
    assert det._freqs.max() < 136.7e6 and det._freqs.min() >= 135.39e6       # VDL2 band left out


# -- scheduler -----------------------------------------------------------------------------------------
def test_segments_cover_the_airband():
    segs = make_segments()
    assert len(segs) == 10 and segs[0].lo == 118e6 and abs(segs[-1].hi - 137e6) < 1
    assert all(abs((s.hi - s.lo) - 1.9e6) < 1 for s in segs)
    assert all(s.hi - s.lo < 2 * 0.42 * FS for s in segs)                     # every segment fits a window


def test_time_follows_activeness():
    segs = make_segments()
    for s in segs:
        s.rate = 0.0
    segs[3].rate, segs[7].rate = 30.0, 6.0
    sch = Scheduler(segs)
    t, spent = 0.0, {s.idx: 0.0 for s in segs}
    for _ in range(3000):
        seg = sch.pick(t)
        t += 10.0
        spent[seg.idx] += 10.0
        seg.credit -= 10.0
        seg.last_visit = t
    share = {k: v / t for k, v in spent.items()}
    want = sch.shares()
    assert abs(share[3] - want[3]) < 0.02 and abs(share[7] - want[7]) < 0.02
    assert share[3] > 3 * share[7] > 0 and min(share.values()) > 0.02           # quiet segments still visited


def test_dwell_rules():
    segs = make_segments()
    sch = Scheduler(segs)
    sch.pick(0.0)
    assert not sch.should_move(3.0, False, False)                              # minimum dwell
    assert sch.should_move(11.0, False, False)
    assert not sch.should_move(11.0, True, False)                              # never cut a transmission
    sch.voice_heard(9.0)
    assert not sch.should_move(11.0, False, False)                             # a conversation is going on
    assert sch.should_move(scanner.MAX_DWELL + 1, False, False)
    assert not sch.should_move(500.0, False, True)                             # someone is listening here
    seg = sch.finish(12.0)
    assert seg.listened_s == 12.0 and seg.rate > scanner.INITIAL_RATE          # 1 transmission in 12 s is busy


def test_smoothing():
    assert smooth(10.0, 10.0, 60) == 10.0
    assert 10.0 < smooth(10.0, 100.0, 60) < smooth(10.0, 100.0, 600) < 100.0


# -- store --------------------------------------------------------------------------------------------
def test_mulaw_roundtrip():
    x = (np.sin(np.linspace(0, 200, 8000)) * 20000).astype(np.int16)
    y = mulaw_decode(mulaw_encode(x))
    assert np.max(np.abs(y.astype(int) - x)) < 0.03 * 32768 and len(mulaw_encode(x)) == len(x)


def test_store_keeps_a_week_and_retires_silent_channels(tmp_path):
    clock = Clock()
    st = Store(tmp_path, retention_days=7, clock=clock)
    st.add_channel("a", 124.35e6, source="detected")
    st.add_channel("m", 121.5e6, "Guard", source="manual", pinned=True)
    pcm = (np.sin(np.arange(12000) / 3) * 8000).astype(np.int16)
    old = st.add_transmission("a", 124.35e6, clock.t - 8 * DAY, pcm, 12000, 20.0)
    new = st.add_transmission("a", 124.35e6, clock.t - 3600, pcm, 12000, 20.0)
    assert abs(new["duration"] - 1.0) < 0.01 and new["bytes"] == 8000
    assert st.wav(new["id"])[:4] == b"RIFF"
    res = st.maintain()
    assert res["expired"] == 1 and st.wav(old["id"]) is None and st.wav(new["id"])
    assert st.channel("a")["status"] == "alive"
    clock.t += DAY + 1                                                          # a day of silence
    assert st.maintain()["retired"] == 1 and st.channel("a")["status"] == "dead"
    assert st.channel("m")["status"] == "alive"                                # pinned channels stay
    st.add_transmission("a", 124.35e6, clock.t, pcm, 12000, 20.0)              # it speaks again
    assert st.channel("a")["status"] == "alive"
    clock.t += 9 * DAY
    st.maintain()
    assert st.channel("a") is None and st.channel("m")                          # long dead and empty: forgotten


def test_store_size_cap_and_coverage(tmp_path):
    clock = Clock()
    st = Store(tmp_path, max_mb=0.02, clock=clock)                              # 20 kB
    st.add_channel("a", 124.35e6)
    pcm = np.zeros(12000, np.int16)
    for i in range(5):
        st.add_transmission("a", 124.35e6, clock.t - 100 + i, pcm, 12000, 10.0)
    assert st.maintain()["trimmed"] == 3 and st.usage_bytes() <= 20_000
    st.add_dwell(100, 110, 123e6, 125e6)
    st.add_dwell(111, 120, 123e6, 125e6)                                        # gap < 2 s: one stretch
    st.add_dwell(200, 210, 123e6, 125e6)
    st.add_dwell(300, 310, 130e6, 132e6)                                        # other window
    assert st.coverage(124.35e6, 0) == [(100, 120), (200, 210)]


# -- scanning end to end -----------------------------------------------------------------------------
def test_scan_finds_records_and_favours_the_busy_segment(tmp_path, monkeypatch):
    # the real dwells (10 s) and smoothing (30 min) shrunk so the test runs in about a minute
    monkeypatch.setattr(scanner, "BASE_DWELL", 2.6)
    monkeypatch.setattr(scanner, "MIN_DWELL", 1.5)
    monkeypatch.setattr(scanner, "MAX_DWELL", 5.0)
    monkeypatch.setattr(scanner, "LINGER_S", 0.5)
    monkeypatch.setattr(scanner, "TAU_S", 20.0)
    monkeypatch.setattr(scanner, "INITIAL_RATE", 5.0)
    busy = snap(124.350e6)                                                     # segment 3: 123.7-125.6 MHz
    srv = FakeSpyServer(Scene([(busy, speech_am(1.2, FS), 0.1, 2.2)]))        # 1.2 s on, 1 s gap
    cfg = RadioConfig(host="127.0.0.1", port=srv.port, mode="scan", vdl2=False, acars=False)
    engine = RadioEngine(ConfigStore(tmp_path / "radio.json", cfg), MessageLog(), Store(tmp_path))
    visits = []
    real_pick = engine.scheduler.pick

    def pick(now, hold=None):
        seg = real_pick(now, hold)
        visits.append(seg.idx)
        return seg
    engine.scheduler.pick = pick
    engine.start()
    try:
        ch = wait_for(lambda: next((c for c in engine.channels() if c["source"] == "detected"), None), timeout=90)
        assert abs(ch["freq_hz"] - busy) < 1
        wait_for(lambda: len(engine.rec.transmissions(ch["id"])) >= 2, timeout=40)
        wait_for(lambda: len(visits) >= 20, timeout=90)
    finally:
        engine.stop()
    seg3 = engine.segments[3]
    others = [s.rate for s in engine.segments if s.idx != 3]
    assert seg3.rate > max(others)
    assert visits.count(3) > len(visits) / 10                                  # more than a fair tenth
    assert engine.rec.coverage(busy, 0)
    assert {s["idx"] for s in engine.snapshot()["scanner"]["segments"]} == set(range(10))
    assert not [c for c in engine.channels() if c["source"] == "detected" and abs(c["freq_hz"] - busy) > 20e3]


def test_broadcasts_do_not_hold_the_scanner():
    sch = Scheduler(make_segments())
    sch.pick(0.0)
    # the session reports "transmitting" only for transmissions younger than LONG_TX_S
    assert not sch.should_move(scanner.MAX_DWELL + 1, True, False)
    assert sch.should_move(scanner.MAX_DWELL + 1, False, False)


def test_a_signal_wider_than_one_step_is_one_channel():
    f = snap(125.725e6)
    wide = speech_am(1.5, FS)
    # the same transmission seen on three neighbouring steps at nearly the same level
    started, ended = run_detector([(f, wide, 0.5), (f + RASTER, wide * 0.9, 0.5), (f - RASTER, wide * 0.85, 0.5)],
                                  center=125.4e6)
    assert [round(b.freq) for b in started] == [round(f)]


def test_broadband_interference_is_not_a_channel():
    rng = np.random.default_rng(3)
    burst = (rng.normal(0, 0.05, int(1.5 * FS)) + 1j * rng.normal(0, 0.05, int(1.5 * FS))).astype(np.complex64)
    from scipy import signal as sig
    taps = sig.firwin(255, 30e3, fs=FS)                         # ~60 kHz wide noise burst
    burst = sig.lfilter(taps, 1, burst).astype(np.complex64) * 4
    started, _ = run_detector([(snap(124.6e6), burst, 0.5)])
    assert started == []


def test_store_merges_spill_over_channels(tmp_path):
    clock = Clock()
    st = Store(tmp_path, clock=clock)
    pcm = np.zeros(8000, np.int16)
    st.add_channel("main", 125.725e6, source="detected")
    st.add_channel("spill", 125.725e6 + RASTER, source="detected")
    st.add_channel("own", 125.725e6 + 2 * RASTER, source="detected")
    st.add_channel("pin", 125.725e6 - RASTER, source="detected", pinned=True)
    for t in (100, 200, 300):
        st.add_transmission("main", 125.725e6, clock.t - t, pcm, 8000, 10)
    for t in (100.4, 199.8):
        st.add_transmission("spill", 125.733e6, clock.t - t, pcm, 8000, 8)
        st.add_transmission("pin", 125.717e6, clock.t - t, pcm, 8000, 8)
    st.add_transmission("own", 125.742e6, clock.t - 150, pcm, 8000, 9)              # talks on its own
    assert st.maintain()["merged"] == 1
    assert {c["id"] for c in st.channels()} == {"main", "own", "pin"}
