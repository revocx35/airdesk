"""Sign-in for a FastAPI app: pages, API, session middleware, security headers.

Everything except the sign-in page and /healthz needs a session. While no account exists, the
sign-in page offers to create the first one; by default only to visitors on a private network, so an
app reachable from the internet cannot be claimed by a stranger before its owner sets it up. Writes need the
X-App-Request header (browsers cannot add it cross-site without CORS) and are refused
when the browser marks them cross-site. WebSockets check Origin against Host.
"""
from __future__ import annotations

import html
import ipaddress
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .auth import AccountError, LoginThrottle, Sessions, UserStore

log = logging.getLogger("sdrcommon.auth")
STATIC = Path(__file__).parent / "static"
COOKIE = "sdr_session"
HEADER = "x-app-request"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
PRIVATE_NETWORKS = "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,::1,fc00::/7"
# where a first-run setup may come from: private ranges, loopback and link-local
LOCAL_NETS = [ipaddress.ip_network(n) for n in (*PRIVATE_NETWORKS.split(","), "169.254.0.0/16", "fe80::/10")]


def trusted_proxies(prefix: str) -> str:
    """Addresses whose X-Forwarded-For/-Proto are believed: "private" (default), "none" or IPs/CIDRs."""
    value = os.environ.get(f"{prefix}_TRUSTED_PROXIES", "private").strip()
    return {"private": PRIVATE_NETWORKS, "none": ""}.get(value.lower(), value)


class _Login(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)


class _Setup(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)


class _PasswordChange(BaseModel):
    current: str = Field(max_length=1024)
    new: str = Field(max_length=1024)


@dataclass
class Auth:
    users: UserStore
    sessions: Sessions
    throttle: LoginThrottle
    cookie: str = COOKIE
    public: set = field(default_factory=set)

    def ws_user(self, ws: WebSocket) -> str | None:
        """The signed-in user of a WebSocket handshake, or None. Checks Origin against Host."""
        origin, host = ws.headers.get("origin"), ws.headers.get("host")
        if not origin or urlsplit(origin).netloc != host:
            return None
        return self.sessions.user(ws.cookies.get(self.cookie))

    def ws_token(self, ws: WebSocket) -> str | None:
        return ws.cookies.get(self.cookie)


def install_auth(app: FastAPI, *, app_name: str, prefix: str, users: UserStore | None = None,
                 sessions: Sessions | None = None, throttle: LoginThrottle | None = None,
                 extra_csp: dict[str, str] | None = None, public: set[str] | None = None) -> Auth:
    env = os.environ.get
    data = Path(env(f"{prefix}_DATA", "/data"))
    if users is None:
        users = UserStore(data / "users.json")
        name, pw = env(f"{prefix}_ADMIN_USER"), env(f"{prefix}_ADMIN_PASSWORD")
        if name and pw and name not in users.names():
            users.add(name, pw)
            log.info("created account %s from %s_ADMIN_USER", name, prefix)
    throttle = throttle or LoginThrottle()
    sessions = sessions or Sessions(users, max_age_s=float(env(f"{prefix}_SESSION_DAYS", "7")) * 86400)
    secure_mode = env(f"{prefix}_SECURE_COOKIES", "auto").lower()
    setup_from = env(f"{prefix}_SETUP_FROM", "private").lower()
    auth = Auth(users, sessions, throttle,
                public={"/login", "/api/login", "/api/login-info", "/api/setup", "/healthz", "/auth/login.js",
                        "/auth/login.css", *(public or set())})
    extra_csp = extra_csp or {}

    def secure(request: Request) -> bool:
        return secure_mode == "true" or (secure_mode == "auto" and request.url.scheme == "https")

    def may_set_up(request: Request) -> bool:
        """First-run setup is offered to private-network visitors (or anyone with *_SETUP_FROM=any)."""
        if setup_from == "any":
            return True
        try:
            ip = ipaddress.ip_address(request.client.host if request.client else "")
        except ValueError:
            return False
        return any(ip in net for net in LOCAL_NETS)

    def csp(host: str) -> str:
        parts = {
            "default-src": "'self'",
            "img-src": "'self' blob: data:",
            "media-src": "'self' blob:",
            "style-src": "'self'",
            "script-src": "'self'",
            "connect-src": f"'self' ws://{host} wss://{host}",
            "frame-ancestors": "'none'",
            "base-uri": "'none'",
            "form-action": "'self'",
        }
        for k, v in extra_csp.items():
            parts[k] = f"{parts.get(k, '')} {v}".strip()
        return "; ".join(f"{k} {v}" for k, v in parts.items())

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method in UNSAFE:
            site = request.headers.get("sec-fetch-site", "same-origin")
            if request.headers.get(HEADER) != "1" or site not in ("same-origin", "none"):
                return JSONResponse({"detail": "Request refused."}, status_code=403)
        path = request.url.path
        if path not in auth.public:
            user = sessions.user(request.cookies.get(COOKIE))
            if user is None:
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Sign in first."}, status_code=401)
                return RedirectResponse("/login", status_code=303)
            request.state.user = user
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = csp(request.headers.get("host", ""))
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        if path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    login_html = (STATIC / "login.html").read_text().replace("{{APP}}", html.escape(app_name))

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request):
        if sessions.user(request.cookies.get(COOKIE)):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(login_html)

    @app.get("/auth/login.js", include_in_schema=False)
    def login_js():
        return FileResponse(STATIC / "login.js", media_type="text/javascript")

    @app.get("/auth/login.css", include_in_schema=False)
    def login_css():
        return FileResponse(STATIC / "login.css", media_type="text/css")

    @app.get("/api/login-info")
    def login_info(request: Request):
        has_users = bool(users.names())
        return {"has_users": has_users, "app": app_name, "setup_allowed": not has_users and may_set_up(request)}

    def start_session(request: Request, resp: Response, user: str) -> None:
        resp.set_cookie(COOKIE, sessions.create(user), max_age=int(sessions.max_age), httponly=True,
                        samesite="strict", secure=secure(request), path="/")

    @app.post("/api/login")
    def login(body: _Login, request: Request, response: Response):
        ip = request.client.host if request.client else "?"
        wait = throttle.retry_after(ip, body.username)
        if wait > 0:
            minutes = max(1, math.ceil(wait / 60))
            return JSONResponse({"detail": f"Too many failed sign-ins. Try again in {minutes} minute"
                                           f"{'s' if minutes != 1 else ''}."},
                                status_code=429, headers={"Retry-After": str(math.ceil(wait))})
        if not users.verify(body.username, body.password):
            throttle.failed(ip, body.username)
            log.warning("failed sign-in for %r from %s", body.username[:64], ip)
            return JSONResponse({"detail": "Wrong username or password."}, status_code=401)
        throttle.succeeded(ip, body.username)
        start_session(request, response, body.username)
        return {"user": body.username}

    @app.post("/api/setup")
    def setup(body: _Setup, request: Request, response: Response):
        if users.names():
            return JSONResponse({"detail": "An account already exists. Sign in instead."}, status_code=409)
        if not may_set_up(request):
            return JSONResponse({"detail": "The first account can only be created from your local network."},
                                status_code=403)
        try:
            users.add_first(body.username.strip(), body.password)
        except AccountError as e:
            return JSONResponse({"detail": str(e)}, status_code=409 if "already exists" in str(e) else 400)
        log.info("first account %s created from %s", body.username[:64], request.client.host if request.client else "?")
        start_session(request, response, body.username.strip())
        return {"user": body.username.strip()}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        sessions.end(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict", secure=secure(request))
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        return {"user": request.state.user}

    @app.post("/api/password")
    def change_password(body: _PasswordChange, request: Request, response: Response):
        user, ip = request.state.user, request.client.host if request.client else "?"
        if throttle.retry_after(ip, user) > 0:
            return JSONResponse({"detail": "Too many failed attempts. Try again later."}, status_code=429)
        if not users.verify(user, body.current):
            throttle.failed(ip, user)
            return JSONResponse({"detail": "The current password is wrong."}, status_code=400)
        try:
            users.set_password(user, body.new)
        except AccountError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        start_session(request, response, user)
        return {"ok": True}

    return auth
