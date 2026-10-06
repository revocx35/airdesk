"""Accounts, password hashing, login throttling and sessions."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import deque
from pathlib import Path

MIN_PASSWORD = 10
MAX_PASSWORD = 256
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_N, _R, _P = 2 ** 15, 8, 1                       # scrypt cost: 32 MiB, ~0.1 s


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=64 << 20, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        kind, n, r, p, salt, dk = encoded.split("$")
        if kind != "scrypt":
            return False
        want = base64.b64decode(dk)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 << 20, dklen=len(want))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


# compared against when the username does not exist, so both cases take the same time
_DUMMY = hash_password(secrets.token_urlsafe(16))


class AccountError(ValueError):
    pass


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"Passwords need at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise AccountError(f"Passwords can have at most {MAX_PASSWORD} characters")


class UserStore:
    """users.json with hashed passwords. Re-read when another process (the CLI) changes it."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._users: dict[str, dict] = {}
        self._mtime: float | None = None

    def _load(self) -> None:
        try:
            m = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._users, self._mtime = {}, None
            return
        if m != self._mtime:
            self._users = json.loads(self.path.read_text() or "{}").get("users", {})
            self._mtime = m

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"users": self._users}, f, indent=2)
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime_ns

    def names(self) -> list[str]:
        with self._lock:
            self._load()
            return sorted(self._users)

    def password_hash(self, name: str) -> str | None:
        with self._lock:
            self._load()
            u = self._users.get(name)
            return u["hash"] if u else None

    def add(self, name: str, password: str) -> None:
        if not NAME_RE.match(name):
            raise AccountError("Usernames are 1-32 letters, digits, dots, dashes or underscores")
        check_password_rules(password)
        h = hash_password(password)
        with self._lock:
            self._load()
            if name in self._users:
                raise AccountError(f"User {name} already exists")
            self._users[name] = {"hash": h, "created": int(time.time())}
            self._save()

    def add_first(self, name: str, password: str) -> None:
        """Create an account only if none exists yet (first-run setup); two racing requests cannot both win."""
        if not NAME_RE.match(name):
            raise AccountError("Usernames are 1-32 letters, digits, dots, dashes or underscores")
        check_password_rules(password)
        h = hash_password(password)
        with self._lock:
            self._load()
            if self._users:
                raise AccountError("An account already exists. Sign in instead.")
            self._users[name] = {"hash": h, "created": int(time.time())}
            self._save()

    def set_password(self, name: str, password: str) -> None:
        check_password_rules(password)
        h = hash_password(password)
        with self._lock:
            self._load()
            if name not in self._users:
                raise AccountError(f"No user called {name}")
            self._users[name]["hash"] = h
            self._save()

    def remove(self, name: str) -> None:
        with self._lock:
            self._load()
            if self._users.pop(name, None) is None:
                raise AccountError(f"No user called {name}")
            self._save()

    def verify(self, name: str, password: str) -> bool:
        h = self.password_hash(name) if len(password) <= MAX_PASSWORD else None
        ok = verify_password(password[:MAX_PASSWORD], h or _DUMMY)
        return ok and h is not None


class LoginThrottle:
    """Limits failed logins per client address, per account and overall, in a sliding window."""

    def __init__(self, window_s: float = 900, per_ip: int = 10, per_account: int = 5, total: int = 100,
                 clock=time.monotonic):
        self.window, self.clock = window_s, clock
        self.limits = {"ip": per_ip, "account": per_account, "total": total}
        self._fails: dict[tuple[str, str], deque] = {}
        self._lock = threading.Lock()

    def _recent(self, key) -> deque:
        q = self._fails.setdefault(key, deque())
        cutoff = self.clock() - self.window
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def retry_after(self, ip: str, account: str) -> float:
        """Seconds until another attempt is allowed; 0 when allowed now."""
        with self._lock:
            wait = 0.0
            for kind, key in (("ip", ip), ("account", account.lower()), ("total", "")):
                q = self._recent((kind, key))
                if len(q) >= self.limits[kind]:
                    wait = max(wait, q[len(q) - self.limits[kind]] + self.window - self.clock())
            return wait

    def failed(self, ip: str, account: str) -> None:
        with self._lock:
            now = self.clock()
            for key in (("ip", ip), ("account", account.lower()), ("total", "")):
                self._recent(key).append(now)

    def succeeded(self, ip: str, account: str) -> None:
        # only the account's count is cleared: one valid login must not reset the address's count,
        # or an attacker with any account could keep guessing other accounts' passwords
        with self._lock:
            self._fails.pop(("account", account.lower()), None)


class Sessions:
    """Server-side sessions. A session ends when it expires, on logout, or when the password changes."""

    def __init__(self, store: UserStore, max_age_s: float = 7 * 86400, clock=time.time):
        self.store, self.max_age, self.clock = store, max_age_s, clock
        self._s: dict[str, dict] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, user: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._s[self._key(token)] = {"user": user, "expires": self.clock() + self.max_age,
                                        "pw": self.store.password_hash(user)}
        return token

    def user(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._lock:
            s = self._s.get(self._key(token))
            if s is None:
                return None
            if s["expires"] < self.clock() or self.store.password_hash(s["user"]) != s["pw"]:
                del self._s[self._key(token)]
                return None
            return s["user"]

    def end(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._s.pop(self._key(token), None)
