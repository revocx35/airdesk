# AGENTS.md

Notes for AI agents working on this repository. Design and threat model: [ARCHITECTURE.md](ARCHITECTURE.md);
user guide: [README.md](README.md).

## Layout
- `common/sdrcommon/` shared by both apps: `auth.py` (scrypt, `UserStore`, `LoginThrottle`, in-memory `Sessions`),
  `webauth.py` (`install_auth`: sign-in pages/API, session middleware, CSRF header check, security headers),
  `users.py` (account CLI), `spyserver.py` (SpyServer client), `static/` (sign-in page). Tests in `common/tests/`.
- `airdesk/airdesk/` Airdesk: `web.py` (FastAPI + `/ws`), `radio.py` (`RadioEngine` worker thread, scanner session),
  `detector.py`, `dsp.py`, `acars.py`, `vdl2.py` (dumpvdl2 bridge), `adsb.py` (tar1090 reader), `store.py`
  (SQLite + recordings), `messages.py`, `static/` (vanilla JS, vendored Leaflet). Tests in `airdesk/tests/`.
- `spyswitch/spyswitch/` SpySwitch: `switchboard.py` (asyncio TCP proxy + app registry), `web.py`. Tests in `spyswitch/tests/`.
- `conftest.py` puts `common`, `spyswitch`, `airdesk` and `common/tests` on `sys.path`.

## Commands
- `python -m pytest -q` from the repo root (needs `requirements-dev.txt`; ~90 s, no radio needed — fake SpyServer).
- `docker build -f airdesk/Dockerfile --target test .` / `-f spyswitch/Dockerfile --target test .` (heavy: builds dumpvdl2).
- Accounts: `docker compose exec airdesk python -m sdrcommon.users add NAME` (same for spyswitch).

## Conventions
- Every route except the sign-in page and `/healthz` needs a session (`install_auth` middleware). Writes need the
  `X-App-Request: 1` header and a same-origin `Sec-Fetch-Site`; WebSockets check Origin against Host and re-check
  the session while open.
- Each app has its own session cookie, `<prefix>_session` (both apps share a host; cookies ignore ports).
- Start/stop of radio workers is serialised (`RadioEngine._control`); web handlers run in parallel threads.
- Frontend: no inline JS/CSS (CSP), build DOM without `innerHTML` for data.

## Never do
- Share one cookie name between the apps; trust `X-Forwarded-For` beyond `*_TRUSTED_PROXIES`; offer first-run setup
  to non-local addresses by default; commit `.env`, `/data` contents or real LAN addresses (the compose default
  `AIRDESK_READSB_URL` is an example only).

## Gotchas
- SpySwitch identifies apps by the client name in the SpyServer HELLO plus the source IP; that is not
  authentication (a client can pick another name).
- Sessions are in memory: restarting a container signs everyone out.
