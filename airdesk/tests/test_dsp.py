import numpy as np
import pytest
from scipy import signal

from airdesk import acars
from airdesk.dsp import AUDIO_RATE, BurstCatcher, Channelizer, VoiceChannel
from airdesk.vdl2 import StreamResampler, normalize

FS = 2_400_000


def tone(freq, n, amp=0.3, fs=FS):
    return (amp * np.exp(2j * np.pi * freq * np.arange(n) / fs)).astype(np.complex64)


def test_stream_resampler_matches_one_shot():
    rng = np.random.default_rng(1)
    x = (rng.normal(size=30_000) + 1j * rng.normal(size=30_000)).astype(np.complex64)
    r = StreamResampler(7, 16)
    parts, i = [], 0
    for n in (1, 5000, 333, 12_000, 7, 12_659):
        parts.append(r.process(x[i:i + n]))
        i += n
    got = np.concatenate(parts)
    ref = signal.upfirdn(r.h, x, 7, 16)
    assert len(got) > 13_000
    assert np.allclose(got, ref[:len(got)], atol=1e-3)


def test_channelizer_picks_out_each_channel():
    ch = Channelizer(FS)
    ch.set_channels({"a": -300_000, "b": 450_000})
    x = tone(-300_000 + 1_000, FS // 10) + tone(450_000 - 2_000, FS // 10, amp=0.1) + tone(700_000, FS // 10, amp=1.0)
    out = {k: [] for k in "ab"}
    for i in range(0, len(x), 120_000):                       # streaming in 50 ms chunks
        for k, v in ch.process(x[i:i + 120_000]).items():
            out[k].append(v)
    a, b = np.concatenate(out["a"]), np.concatenate(out["b"])
    assert ch.out_rate == AUDIO_RATE and abs(len(a) - 1200) <= 20
    assert np.mean(np.abs(a[40:])) == pytest.approx(0.3, rel=0.05)       # its own tone, at full level
    assert np.mean(np.abs(b[40:])) == pytest.approx(0.1, rel=0.08)       # the strong tone 250 kHz away is gone
    f = np.fft.fftfreq(len(a) - 40, 1 / AUDIO_RATE)[np.argmax(np.abs(np.fft.fft(a[40:])))]
    assert abs(f - 1_000) < 50


def am_voice(seconds, rate=AUDIO_RATE, carrier=0.2, start=0.5, stop=1.5, noise=0.004, seed=0):
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    t = np.arange(n) / rate
    on = (t >= start) & (t < stop)
    audio = 0.6 * np.sin(2 * np.pi * 700 * t)
    x = np.where(on, carrier * (1 + audio), 0) + noise * (rng.normal(size=n) + 1j * rng.normal(size=n))
    return x.astype(np.complex64)


def test_voice_squelch_audio_and_clip():
    v = VoiceChannel("tower", 118.1e6)
    x = am_voice(3.0)
    pcm, clips = [], []
    for i in range(0, len(x), 600):
        p, c = v.process(x[i:i + 600], now=i / AUDIO_RATE)
        pcm.append(p)
        if c:
            clips.append(c)
    pcm = np.concatenate(pcm).astype(float)
    t = np.arange(len(pcm)) / AUDIO_RATE
    assert np.abs(pcm[t < 0.45]).max() == 0                                # silent before the transmission
    assert np.abs(pcm[(t > 2.3)]).max() == 0                                 # and after (after the hang time)
    spec = np.abs(np.fft.rfft(pcm[(t > 0.7) & (t < 1.3)]))
    assert abs(np.fft.rfftfreq(int(((t > 0.7) & (t < 1.3)).sum()), 1 / AUDIO_RATE)[spec.argmax()] - 700) < 20
    assert len(clips) == 1 and 0.9 < clips[0].duration < 1.4 and clips[0].level_db > 20
    assert clips[0].wav()[:4] == b"RIFF"


def test_short_blips_are_not_kept_as_clips():
    v = VoiceChannel("x", 1.0)
    x = am_voice(2.0, start=0.5, stop=0.6)
    clips = [c for i in range(0, len(x), 600) if (c := v.process(x[i:i + 600], 0)[1])]
    assert clips == []


@pytest.mark.parametrize("snr_noise, drift", [(0.01, 0.0), (0.03, 0.0004), (0.03, -0.0004)])
def test_acars_roundtrip(snr_noise, drift):
    frame = acars.encode("2", "N123AB", "H1", "3", text="POS N5000E01000,,123456,FL370", msg_num="D01A", flight="TST123")
    burst = acars.modulate(frame, AUDIO_RATE)
    if drift:                                                               # transmitter clock off by drift
        n = len(burst)
        burst = np.interp(np.arange(0, n - 1, 1 + drift), np.arange(n), burst.real).astype(np.complex64)
    rng = np.random.default_rng(3)
    x = np.concatenate([np.zeros(600), burst, np.zeros(600)]).astype(np.complex64)
    x += (snr_noise * (rng.normal(size=len(x)) + 1j * rng.normal(size=len(x)))).astype(np.complex64)
    msgs = acars.Demodulator(AUDIO_RATE).decode(x)
    assert len(msgs) == 1
    m = msgs[0]
    assert (m.reg, m.flight, m.label, m.msg_num, m.block_id) == ("N123AB", "TST123", "H1", "D01A", "3")
    assert m.text == "POS N5000E01000,,123456,FL370" and m.label_name == "Message to/from terminal"


def test_acars_corrects_a_single_bit_error():
    frame = bytearray(acars.encode("2", "N123AB", "_\x7f", "5"))
    frame[4] ^= 0x04                                                         # flip one bit in the address
    x = acars.modulate(bytes(frame), AUDIO_RATE)
    m = acars.Demodulator(AUDIO_RATE).decode(x)
    assert len(m) == 1 and m[0].reg == "N123AB" and m[0].corrected == 1


def test_acars_ignores_noise_and_bad_frames():
    rng = np.random.default_rng(9)
    d = acars.Demodulator(AUDIO_RATE)
    for _ in range(10):
        assert d.decode((rng.normal(size=8000) + 1j * rng.normal(size=8000)).astype(np.complex64)) == []
    frame = bytearray(acars.encode("2", "N123AB", "H1", "1", text="HELLO"))
    frame[6] ^= 0x03                                                         # two bits in one char: parity holds, CRC fails
    assert d.decode(acars.modulate(bytes(frame), AUDIO_RATE)) == []


def test_burst_catcher_hands_over_whole_bursts():
    bc = BurstCatcher()
    frame = acars.encode("2", "N456CD", "Q0", "2")
    x = np.concatenate([np.zeros(6000), acars.modulate(frame, AUDIO_RATE), np.zeros(6000)]).astype(np.complex64)
    x += (0.004 * np.random.default_rng(2).normal(size=len(x))).astype(np.complex64)
    bursts = [b for i in range(0, len(x), 600) if (b := bc.process(x[i:i + 600])) is not None]
    assert len(bursts) == 1
    assert acars.Demodulator(AUDIO_RATE).decode(bursts[0])[0].reg == "N456CD"


def test_vdl2_normalize():
    raw = {"vdl2": {"t": {"sec": 100}, "freq": 136975000, "sig_level": -30.0, "noise_level": -47.0,
                    "avlc": {"src": {"addr": "a1b2c3", "type": "Aircraft"}, "frame_type": "I",
                             "acars": {"reg": ".N123AB", "flight": "TST75A", "label": "H1", "msg_text": "HELLO\r"}}}}
    m = normalize(raw)
    assert (m["hex"], m["reg"], m["flight"], m["label"], m["text"], m["level_db"]) == ("A1B2C3", "N123AB", "TST75A", "H1", "HELLO", 17.0)
    assert normalize({"vdl2": {"avlc": {"src": {"type": "Aircraft", "addr": "1"}, "frame_type": "S"}}}) is None
    assert normalize({"vdl2": {"avlc": {"src": {"type": "Aircraft", "addr": "1"}, "frame_type": "I",
                                        "x25": {"pkt_type_name": "Data"}}}}) is None     # housekeeping, no ACARS
    up = normalize({"vdl2": {"avlc": {"src": {"type": "Ground station", "addr": "ABCDEF"}, "frame_type": "I",
                                      "acars": {"reg": ".N123AB", "label": "_\x7f", "msg_text": ""}}}})
    assert up["uplink"] and up["hex"] == "" and up["reg"] == "N123AB" and up["label"] == "_d"
