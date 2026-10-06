import gzip
import json
import os
import stat
import sys
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from fake_spyserver import FS, FakeSpyServer
from airdesk import acars, vdl2
from airdesk.adsb import AdsbFeed, Tar1090Db
from airdesk.messages import MessageLog
from airdesk.radio import ConfigStore, RadioConfig, RadioEngine
from airdesk.store import Store
from airdesk.web import create_app
from sdrcommon.auth import LoginThrottle, Sessions, UserStore

PW = "airdesk password"
H = {"X-App-Request": "1"}


def wait_for(fn, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


# -- ADS-B --------------------------------------------------------------------------------------
DB = {
    "/tar1090/": b'<script>databaseFolder = "db-abc123";</script>',
    "/tar1090/db-abc123/A.js": gzip.compress(json.dumps({"children": ["A1"], "00001": ["N400AA", "A320", "00", "AIRBUS A-320"]}).encode()),
    "/tar1090/db-abc123/A1.js": gzip.compress(json.dumps({"B2C3": ["N123AB", "B38M", "00", "BOEING 737 MAX 8"]}).encode()),
}


def fake_fetch(extra=None):
    def fetch(url, timeout=5):
        path = url.split("example", 1)[1]
        table = {**DB, **(extra or {})}
        if path not in table:
            raise OSError(f"404 {path}")
        v = table[path]
        return gzip.decompress(v) if v[:2] == b"\x1f\x8b" else v
    return fetch


AIRCRAFT = {"now": 1000.0, "messages": 5, "aircraft": [
    {"hex": "a1b2c3", "flight": "TST75   ", "alt_baro": 12000, "gs": 300, "track": 90, "lat": 50.0, "lon": 10.0,
     "seen": 0.5, "seen_pos": 0.5, "squawk": "1234", "category": "A3", "rssi": -12.0, "baro_rate": -640},
    {"hex": "a00001", "alt_baro": "ground", "lat": 50.1, "lon": 10.1, "seen": 1, "seen_pos": 1},
    {"hex": "abcdef", "seen": 0.1},
    {"hex": "dead00", "seen": 120},
]}


def test_tar1090_database_walks_child_files():
    db = Tar1090Db("http://example/tar1090", fake_fetch())
    assert db.lookup("a1b2c3") == {"reg": "N123AB", "type": "B38M", "desc": "BOEING 737 MAX 8"}
    assert db.lookup("a00001")["reg"] == "N400AA"
    assert db.lookup("a1ffff") is None and db.lookup("7c0000") is None


def test_feed_builds_aircraft_trails_and_links():
    feed = AdsbFeed("http://example/tar1090", receiver=(50.0, 9.9), fetch=fake_fetch())
    feed.update(AIRCRAFT)
    ac = {a["hex"]: a for a in feed.snapshot()}
    assert set(ac) == {"a1b2c3", "a00001", "abcdef"}                      # stale aircraft dropped
    a = ac["a1b2c3"]
    assert (a["flight"], a["reg"], a["type"], a["alt"], a["vr"]) == ("TST75", "N123AB", "B38M", 12000, -640)
    assert 6.5 < a["dist_km"] < 7.5 and "lat" not in ac["abcdef"]
    later = json.loads(json.dumps(AIRCRAFT))
    later["now"] = 1001.0
    later["aircraft"][0]["lon"] = 10.01
    feed.update(later)
    assert len(feed.trails_snapshot()["a1b2c3"]) == 2
    assert feed.find(reg="N123AB") == "a1b2c3" and feed.find(hexid="A1B2C3") == "a1b2c3" and feed.find(reg="N1") == ""
    st = feed.status_snapshot()
    assert st["ok"] and st["aircraft"] == 3 and st["with_position"] == 2


def test_messages_link_to_aircraft():
    feed = AdsbFeed("http://example/tar1090", fetch=fake_fetch())
    feed.update(AIRCRAFT)
    log = MessageLog(feed.find)
    got = []
    log.subscribe(got.append)
    m = log.add({"source": "ACARS", "reg": "N123AB", "text": "hi"})
    assert m["aircraft"] == "a1b2c3" and got == [m]
    log.add({"source": "VDL2", "hex": "ABCDEF"})
    assert [x["source"] for x in log.list("a1b2c3")] == ["ACARS"] and log.counts["VDL2"] == 1


# -- radio -------------------------------------------------------------------------------------
def speech_am(seconds: float, fs: float, carrier: float = 0.15) -> np.ndarray:
    """AM with a syllable-like envelope (4 Hz) on a 600 Hz tone, like speech on a keyed carrier."""
    t = np.arange(int(seconds * fs)) / fs
    env = 0.45 * (1 + np.sin(2 * np.pi * 4 * t))
    return (carrier * (1 + env * np.sin(2 * np.pi * 600 * t))).astype(np.complex64)


class Scene:
    """Signals at given airband frequencies, each repeating with a period: [(freq, signal, start_s, period_s)]."""

    def __init__(self, items):
        self.n, self.items = 0, items

    def __call__(self, center, n):
        k = self.n + np.arange(n)
        self.n += n
        x = (np.random.default_rng(self.n).normal(0, 0.004, 2 * n)).astype(np.float32).view(np.complex64)
        for f, sig, start, period in self.items:
            if abs(f - center) > FS / 2:
                continue
            idx = (k % int(period * FS)) - int(start * FS)
            ok = (idx >= 0) & (idx < len(sig))
            if ok.any():
                x[ok] += sig[idx[ok]] * np.exp(2j * np.pi * (f - center) * k[ok] / FS).astype(np.complex64)
        return x


def default_scene():
    frame = acars.encode("2", "N123AB", "H1", "4", text="AIRDESK TEST MESSAGE", msg_num="D05A", flight="TST75")
    return Scene([(131.0e6, speech_am(1.2, FS), 0.2, 3.0), (131.525e6, acars.modulate(frame, FS) * 0.4, 1.8, 3.0)])


@pytest.fixture
def radio_parts(tmp_path):
    srv = FakeSpyServer(default_scene())
    feed = AdsbFeed("http://example/tar1090", fetch=fake_fetch())
    feed.update(AIRCRAFT)
    log = MessageLog(feed.find)
    cfg = RadioConfig(host="127.0.0.1", port=srv.port, mode="fixed", center_hz=131.2e6, gain=20, vdl2=False)
    engine = RadioEngine(ConfigStore(tmp_path / "radio.json", cfg), log, Store(tmp_path))
    engine.add_channel(131.0e6, "Test voice")
    engine.add_channel(118.1e6, "Tower")
    yield srv, feed, log, engine
    engine.shutdown()
    srv.close()


def test_engine_records_voice_and_decodes_acars(radio_parts):
    srv, feed, log, engine = radio_parts
    engine.start()
    msg = wait_for(lambda: next((m for m in log.list() if m["source"] == "ACARS"), None))
    assert (msg["reg"], msg["flight"], msg["text"], msg["aircraft"]) == ("N123AB", "TST75", "AIRDESK TEST MESSAGE", "a1b2c3")
    tx = wait_for(lambda: engine.rec.transmissions())[0]
    voice = next(c for c in engine.channels() if c["label"] == "Test voice")
    assert tx["channel_id"] == voice["id"] and 0.9 < tx["duration"] < 1.6
    assert engine.rec.wav(tx["id"])[:4] == b"RIFF"
    snap = engine.snapshot()
    views = {v["label"]: v for v in snap["channels"]}
    assert views["Test voice"]["inside"] and not views["Tower"]["inside"]
    assert snap["state"] == "running" and srv.hellos[-1] == "airdesk" and snap["scanner"]["current"] is None
    assert not [c for c in engine.channels() if c["source"] == "detected"]   # the voice is already a channel
    engine.update(center_hz=136.4e6, gain=12)
    wait_for(lambda: srv.settings[-1].get(101) == 136_400_000 and srv.settings[-1].get(2) == 12)
    assert json.loads(engine.store.path.read_text())["center_hz"] == 136.4e6
    engine.stop()
    assert engine.rec.coverage(131.0e6, 0)                                    # listening time was booked


def test_engine_reports_unreachable_radio(tmp_path):
    cfg = RadioConfig(host="127.0.0.1", port=1)
    engine = RadioEngine(ConfigStore(tmp_path / "r.json", cfg), MessageLog(), Store(tmp_path))
    engine.start()
    try:
        st = wait_for(lambda: engine.snapshot()["state"] == "error" and engine.snapshot())
        assert "Cannot connect" in st["message"]
    finally:
        engine.stop()
    assert engine.snapshot()["state"] == "stopped"


def test_old_channel_list_moves_into_the_store(tmp_path):
    (tmp_path / "radio.json").write_text(json.dumps({"center_hz": 131.2e6, "channels": [
        {"id": "c1", "freq_hz": 131.0e6, "label": "Approach", "squelch_db": 9.0}]}))
    engine = RadioEngine(ConfigStore(tmp_path / "radio.json", RadioConfig()), MessageLog(), Store(tmp_path))
    c = engine.channels()[0]
    assert (c["label"], c["pinned"], c["source"], c["squelch_db"]) == ("Approach", 1, "manual", 9.0)
    assert json.loads((tmp_path / "radio.json").read_text())["channels"] == []


def test_vdl2_bridge_plumbing(tmp_path):
    fake = tmp_path / "dumpvdl2"
    fake.write_text(f"""#!{sys.executable}
import sys, json
sys.stdin.buffer.read(4000)
print(json.dumps({{"vdl2": {{"t": {{"sec": 5}}, "freq": 136975000, "sig_level": -20, "noise_level": -40,
      "avlc": {{"src": {{"addr": "A1B2C3", "type": "Aircraft"}}, "frame_type": "I",
               "acars": {{"reg": ".N123AB", "flight": "TST75", "label": "H1", "msg_text": "FROM FAKE"}}}}}}}}), flush=True)
sys.stdin.buffer.read()
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    got = []
    b = vdl2.Vdl2Bridge(got.append, binary=str(fake))
    b.start(FS, 136.4e6, [136.975e6, 136.875e6])
    assert b.group == pytest.approx(136.925e6) and b.center == 136.4e6
    for _ in range(5):
        b.feed(np.zeros(120_000, np.complex64))
    wait_for(lambda: got)
    b.stop()
    assert got[0]["text"] == "FROM FAKE" and got[0]["hex"] == "A1B2C3"


# -- web ---------------------------------------------------------------------------------------
def test_web_app(radio_parts, tmp_path):
    srv, feed, log, engine = radio_parts
    users = UserStore(tmp_path / "users.json")
    users.add("pilot", PW)
    app = create_app(feed, engine, log, start_background=False, users=users, sessions=Sessions(users),
                     throttle=LoginThrottle())
    with TestClient(app, headers=H) as c:
        assert c.get("/api/state").status_code == 401
        assert c.post("/api/login", json={"username": "pilot", "password": PW}).status_code == 200
        assert "https://tile.openstreetmap.org" in c.get("/").headers["content-security-policy"]
        assert c.get("/api/state").json()["tiles"]["url"].startswith("https://tile.openstreetmap.org/")
        assert {a["hex"] for a in c.get("/api/aircraft").json()["aircraft"]} == {"a1b2c3", "a00001", "abcdef"}
        r = c.post("/api/channels", json={"freq_mhz": 130.975, "label": "Approach"})
        assert r.status_code == 200
        assert c.post("/api/channels", json={"freq_mhz": 130.975}).status_code == 400
        rows = {x["label"]: x for x in c.get("/api/channels").json()}
        assert rows["Approach"]["pinned"] == 1 and len(rows["Approach"]["last_24h"]) == 24
        cid = rows["Approach"]["id"]
        assert c.post(f"/api/channels/{cid}", json={"label": "APP", "pinned": False}).status_code == 200
        assert next(x for x in c.get("/api/channels").json() if x["id"] == cid)["label"] == "APP"
        assert c.delete(f"/api/channels/{cid}").status_code == 200
        assert c.delete(f"/api/channels/{cid}").status_code == 404
        assert c.post("/api/radio/mode", json={"mode": "sideways"}).status_code == 422
        assert c.post("/api/radio/settings", json={"center_mhz": 5000}).status_code == 422
        c.post("/api/radio/start")
        voice = rows["Test voice"]["id"]
        with c.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello" and len(hello["aircraft"]) == 3 and "transmissions" in hello
            ws.send_json({"listen": [voice]})
            kinds, audio = set(), 0
            end = time.monotonic() + 12
            while time.monotonic() < end and not ({"message", "transmission"} <= kinds and audio > 5):
                m = ws.receive()
                if m.get("bytes"):
                    audio += 1
                elif m.get("text"):
                    kinds.add(json.loads(m["text"])["type"])
            assert {"state", "aircraft", "message", "transmission"} <= kinds and audio > 5
            assert engine.listening() == {voice}
        wait_for(lambda: engine.listening() == set())
        rec = c.get("/api/recordings").json()[0]
        assert rec["label"] == "Test voice"
        assert c.get(f"/api/recordings/{rec['id']}.wav").headers["content-type"] == "audio/wav"
        hist = c.get(f"/api/channels/{voice}/history", params={"hours": 24}).json()
        assert hist["transmissions"] and hist["channel"]["label"] == "Test voice"
        assert hist["coverage"] and hist["coverage"][-1][1] >= hist["until"] - 1      # includes listening right now
        assert c.get("/api/messages", params={"aircraft": "a1b2c3"}).json()[0]["reg"] == "N123AB"
        assert c.post("/api/radio/mode", json={"mode": "scan"}).json()["radio"]["config"]["mode"] == "scan"
        c.post("/api/radio/stop")


@pytest.mark.skipif(not vdl2.available(), reason="dumpvdl2 is only installed in the Docker image")
def test_real_dumpvdl2_accepts_our_command_line():
    b = vdl2.Vdl2Bridge(lambda m: None)
    b.start(FS, 136.4e6, list(vdl2.VDL2_FREQS))
    try:
        for _ in range(20):
            b.feed((np.random.default_rng(1).normal(0, 0.01, 240_000)).astype(np.float32).view(np.complex64))
            time.sleep(0.05)
        assert b.running
    finally:
        b.stop()


def test_tile_origin():
    from airdesk.web import tile_origin
    assert tile_origin("https://tile.openstreetmap.org/{z}/{x}/{y}.png") == "https://tile.openstreetmap.org"
    assert tile_origin("https://{s}.tiles.example.org/{z}/{x}/{y}.png") == "https://*.tiles.example.org"


def test_messages_survive_a_restart(tmp_path):
    path = tmp_path / "messages.jsonl"
    log = MessageLog(path=path, maxlen=5)
    for i in range(12):
        log.add({"source": "VDL2", "text": f"m{i}"})
    again = MessageLog(path=path, maxlen=5)
    assert [m["text"] for m in again.list()] == ["m11", "m10", "m9", "m8", "m7"]
    assert again.add({"source": "ACARS", "text": "new"})["id"] == 13
    assert len(path.read_text().splitlines()) <= 25                    # compacted, not growing forever
