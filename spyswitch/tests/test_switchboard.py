import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from fake_spyserver import FakeSpyServer, free_port
from sdrcommon.auth import LoginThrottle, Sessions, UserStore
from sdrcommon.spyserver import SourceError, SpyServerSource
from spyswitch.switchboard import Registry, Switchboard, _CommandWatcher, Conn, parse_servers
from spyswitch.web import create_app

PW = "switch password"
H = {"X-App-Request": "1"}


def wait_for(fn, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.03)
    raise AssertionError("condition not met in time")


@pytest.fixture
def upstream():
    srv = FakeSpyServer()
    yield srv
    srv.close()


@pytest.fixture
def board(tmp_path, upstream):
    """A switchboard running on its own event loop thread."""
    port = free_port()
    sb = Switchboard(parse_servers(f"Main@{port}=127.0.0.1:{upstream.port}"), Registry(tmp_path / "apps.json"),
                     "127.0.0.1")
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    asyncio.run_coroutine_threadsafe(sb.start(), loop).result(5)
    sb.port, sb.loop = port, loop
    yield sb
    asyncio.run_coroutine_threadsafe(sb.stop(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)


def call(sb, fn, *args):
    """Run a switchboard method on its loop thread, as the web layer does."""
    async def go():
        return fn(*args)
    return asyncio.run_coroutine_threadsafe(go(), sb.loop).result(5)


def client(port, name):
    src = SpyServerSource("127.0.0.1", port, client_name=name, timeout=3)
    src.open()
    return src


def test_parse_servers():
    s = parse_servers("VHF/UHF@5555=127.0.0.1:15555; HF @ 5556 = [::1]:15556")
    assert [(x.name, x.listen_port, x.upstream_host, x.upstream_port) for x in s] == \
        [("VHF/UHF", 5555, "127.0.0.1", 15555), ("HF", 5556, "::1", 15556)]
    with pytest.raises(ValueError, match="Cannot read"):
        parse_servers("nonsense")
    with pytest.raises(ValueError, match="same listening port"):
        parse_servers("A@1=h:2; B@1=h:3")


def test_command_watcher_handles_split_stream():
    import struct
    c = Conn(1, "s", "a", "a", "ip")
    w = _CommandWatcher(c)
    data = (struct.pack("<II", 2, 8) + struct.pack("<II", 101, 136_400_000) +
            struct.pack("<II", 2, 8) + struct.pack("<II", 2, 17) + struct.pack("<II", 2, 8) + struct.pack("<II", 1, 1))
    for i in range(0, len(data), 5):
        w.feed(data[i:i + 5])
    assert (c.freq, c.gain, c.streaming) == (136_400_000, 17, True)


def test_proxy_identifies_app_and_follows_its_settings(board, upstream):
    src = client(board.port, "airdesk")
    src.tune(136.4e6)
    src.set_gain(20)
    src.start()
    x = src.read(50_000)
    assert len(x) == 50_000
    snap = wait_for(lambda: (s := board.snapshot())["servers"][0]["connections"] and
                    s["servers"][0]["connections"][0]["streaming"] and s)
    c = snap["servers"][0]["connections"][0]
    assert c["name"] == "airdesk" and c["ip"] == "127.0.0.1" and c["freq"] == 136_400_000 and c["gain"] == 20
    assert snap["apps"][0]["key"] == "airdesk@127.0.0.1" and snap["apps"][0]["allowed"] is True
    assert upstream.hellos[-1] == "airdesk"
    src.close()
    wait_for(lambda: not board.snapshot()["servers"][0]["connections"])
    assert "airdesk (127.0.0.1) left Main" in [e["text"] for e in board.snapshot()["events"]]


def test_switching_an_app_off_kicks_and_refuses_it(board):
    a = client(board.port, "atvrx")
    a.start()
    a.read(10_000)
    call(board, board.set_allowed, "atvrx@127.0.0.1", False)
    with pytest.raises(SourceError):
        for _ in range(200):
            a.read(20_000)
    a.close()
    with pytest.raises(SourceError, match="refused this app"):
        client(board.port, "atvrx")
    b = client(board.port, "airdesk")                       # another app is still welcome
    b.close()
    call(board, board.set_allowed, "atvrx@127.0.0.1", True)
    client(board.port, "atvrx").close()
    kinds = [e["kind"] for e in board.snapshot()["events"]]
    assert "refused" in kinds


def test_disconnect_once_without_blocking(board):
    a = client(board.port, "SDR#")
    cid = wait_for(lambda: board.snapshot()["servers"][0]["connections"])[0]["id"]
    call(board, board.disconnect, cid)
    wait_for(lambda: not board.snapshot()["servers"][0]["connections"])
    a.close()
    client(board.port, "SDR#").close()                      # still allowed


def test_new_apps_can_be_blocked_by_default(board):
    call(board, board.set_default_allow, False)
    with pytest.raises(SourceError, match="refused"):
        client(board.port, "newcomer")
    assert board.registry.apps["newcomer@127.0.0.1"]["allowed"] is False


def test_registry_survives_restart(tmp_path):
    r = Registry(tmp_path / "apps.json")
    r.seen("x@1.2.3.4", "x", "1.2.3.4", "5555")
    r.apps["x@1.2.3.4"]["allowed"] = False
    r.default_allow = False
    r.save()
    r2 = Registry(tmp_path / "apps.json")
    assert r2.apps["x@1.2.3.4"]["allowed"] is False and r2.default_allow is False


def test_upstream_down_is_reported(tmp_path):
    port = free_port()
    sb = Switchboard(parse_servers(f"Main@{port}=127.0.0.1:{free_port()}"), Registry(tmp_path / "a.json"), "127.0.0.1")
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    asyncio.run_coroutine_threadsafe(sb.start(), loop).result(5)
    try:
        with pytest.raises(SourceError):
            client(port, "atvrx")
        wait_for(lambda: sb.snapshot()["servers"][0]["upstream_ok"] is False)
    finally:
        asyncio.run_coroutine_threadsafe(sb.stop(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)


def test_web_api_controls_the_switchboard(tmp_path, upstream):
    port = free_port()
    sb = Switchboard(parse_servers(f"Main@{port}=127.0.0.1:{upstream.port}"), Registry(tmp_path / "apps.json"),
                     "127.0.0.1")
    users = UserStore(tmp_path / "users.json")
    users.add("admin", PW)
    app = create_app(sb, users=users, sessions=Sessions(users), throttle=LoginThrottle())
    with TestClient(app, headers=H) as c:
        assert c.get("/api/state").status_code == 401
        assert c.post("/api/login", json={"username": "admin", "password": PW}).status_code == 200
        src = client(port, "atvrx")
        st = wait_for(lambda: (s := c.get("/api/state").json())["servers"][0]["connections"] and s)
        assert st["apps"][0]["connected"] == [str(port)]
        r = c.post("/api/apps/allowed", json={"key": "atvrx@127.0.0.1", "allowed": False})
        assert r.status_code == 200
        wait_for(lambda: not c.get("/api/state").json()["servers"][0]["connections"])
        src.close()
        assert c.post("/api/apps/allowed", json={"key": "ghost@1.1.1.1", "allowed": True}).status_code == 404
        assert c.post("/api/apps/forget", json={"key": "atvrx@127.0.0.1"}).status_code == 200
        assert c.get("/api/state").json()["apps"] == []
        with c.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
            assert "servers" in ws.receive_json()
        assert c.get("/").status_code == 200 and c.get("/docs").status_code in (401, 404)
