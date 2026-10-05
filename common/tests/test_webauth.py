import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from sdrcommon.auth import AccountError, LoginThrottle, Sessions, UserStore, hash_password, verify_password
from sdrcommon.webauth import install_auth, trusted_proxies

PW = "correct horse battery"
H = {"X-App-Request": "1"}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_app(tmp_path, clock=None):
    users = UserStore(tmp_path / "users.json")
    if "alice" not in users.names():
        users.add("alice", PW)
    clock = clock or Clock()
    app = FastAPI()
    auth = install_auth(app, app_name="Testapp", prefix="TESTAPP", users=users,
                        throttle=LoginThrottle(clock=clock), sessions=Sessions(users, 3600, clock=clock),
                        extra_csp={"img-src": "https://tiles.example"})

    @app.get("/")
    def index():
        return {"page": "app"}

    @app.get("/api/data")
    def data():
        return {"secret": 42}

    @app.post("/api/do")
    def do():
        return {"done": True}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        if auth.ws_user(sock) is None:
            await sock.close(code=4401)
            return
        await sock.accept()
        await sock.send_json({"hello": auth.ws_user(sock)})
        await sock.close()

    return app, auth, clock


@pytest.fixture
def client(tmp_path):
    app, auth, clock = make_app(tmp_path)
    with TestClient(app, headers=H) as c:
        c.clock, c.app_auth = clock, auth
        yield c


def login(c, name="alice", pw=PW):
    return c.post("/api/login", json={"username": name, "password": pw})


def test_hash_and_store(tmp_path):
    h = hash_password(PW)
    assert verify_password(PW, h) and not verify_password("nope", h) and not verify_password(PW, "junk")
    s = UserStore(tmp_path / "u.json")
    s.add("bob", PW)
    with pytest.raises(AccountError):
        s.add("bob", PW)
    with pytest.raises(AccountError, match="at least 10"):
        s.add("carl", "short")
    assert (tmp_path / "u.json").stat().st_mode & 0o077 == 0
    assert PW not in (tmp_path / "u.json").read_text()
    assert UserStore(tmp_path / "u.json").verify("bob", PW) and not s.verify("nobody", PW)


def test_everything_but_sign_in_is_closed(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/api/data").status_code == 401
    assert client.post("/api/do").status_code == 401
    assert "Testapp" in client.get("/login").text
    assert client.get("/auth/login.js").status_code == 200
    assert client.get("/api/login-info").json() == {"has_users": True, "app": "Testapp"}
    with pytest.raises(Exception):
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}):
            pass


def test_sign_in_flow_and_headers(client):
    assert login(client, pw="wrong one!").status_code == 401
    r = login(client)
    assert r.status_code == 200
    ck = r.headers["set-cookie"].lower()
    assert "httponly" in ck and "samesite=strict" in ck and "secure" not in ck
    r = client.get("/api/data")
    assert r.json() == {"secret": 42} and r.headers["cache-control"] == "no-store"
    csp = client.get("/").headers["content-security-policy"]
    assert "img-src 'self' blob: data: https://tiles.example" in csp and "frame-ancestors 'none'" in csp
    with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_json() == {"hello": "alice"}
    with pytest.raises(Exception):
        with client.websocket_connect("/ws", headers={"origin": "http://evil.example"}):
            pass
    assert client.post("/api/do", headers={"X-App-Request": ""}).status_code == 403
    assert client.post("/api/do", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/do").json() == {"done": True}
    client.post("/api/logout")
    assert client.get("/api/data").status_code == 401


def test_secure_cookie_over_https(tmp_path):
    app, _, _ = make_app(tmp_path)
    with TestClient(app, base_url="https://testserver", headers=H) as c:
        assert "secure" in login(c).headers["set-cookie"].lower()


def test_throttle_through_the_api(client):
    for _ in range(5):
        assert login(client, pw="wrong password").status_code == 401
    r = login(client)
    assert r.status_code == 429 and "15 minutes" in r.json()["detail"]
    client.clock.t += 901
    assert login(client).status_code == 200


def test_throttle_rules():
    clock = Clock()
    t = LoginThrottle(window_s=900, per_ip=10, per_account=5, total=100, clock=clock)
    for i in range(10):
        t.failed("1.1.1.1", f"u{i}")
    t.succeeded("1.1.1.1", "mine")
    assert t.retry_after("1.1.1.1", "other") > 0          # a good login does not reset the address
    assert t.retry_after("2.2.2.2", "other") == 0
    g = LoginThrottle(window_s=900, per_ip=99, per_account=99, total=3, clock=Clock())
    for i in range(3):
        g.failed(f"10.0.0.{i}", f"x{i}")
    assert g.retry_after("10.9.9.9", "fresh") > 0


def test_password_change_signs_out_elsewhere(tmp_path):
    app, auth, _ = make_app(tmp_path)
    with TestClient(app, headers=H) as a, TestClient(app, headers=H) as b:
        login(a), login(b)
        r = a.post("/api/password", json={"current": PW, "new": "short"})
        assert r.status_code == 400 and "at least 10" in r.json()["detail"]
        assert a.post("/api/password", json={"current": PW, "new": "another long one"}).status_code == 200
        assert b.get("/api/data").status_code == 401 and a.get("/api/data").status_code == 200


def test_admin_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTAPP_DATA", str(tmp_path))
    monkeypatch.setenv("TESTAPP_ADMIN_USER", "admin")
    monkeypatch.setenv("TESTAPP_ADMIN_PASSWORD", PW)
    auth = install_auth(FastAPI(), app_name="T", prefix="TESTAPP")
    assert auth.users.verify("admin", PW)


def test_proxy_trust(monkeypatch):
    from uvicorn.middleware.proxy_headers import _TrustedHosts
    hosts = _TrustedHosts(trusted_proxies("TESTAPP"))
    assert hosts.get_trusted_client_address("6.6.6.6, 1.2.3.4, 192.168.1.30")[0] == "1.2.3.4"
    monkeypatch.setenv("TESTAPP_TRUSTED_PROXIES", "none")
    assert trusted_proxies("TESTAPP") == ""


def test_users_cli(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {"APP_DATA": str(tmp_path), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(root)}
    run = lambda *a, stdin="": subprocess.run([sys.executable, "-m", "sdrcommon.users", *a], input=stdin, text=True,
                                              capture_output=True, env=env)
    assert run("add", "zoe", "--password-stdin", stdin=PW + "\n").returncode == 0
    assert run("list").stdout.split() == ["zoe"]
    assert run("add", "yan", "--password-stdin", stdin="short\n").returncode == 1
    assert json.loads((tmp_path / "users.json").read_text())["users"]["zoe"]["hash"].startswith("scrypt$")
