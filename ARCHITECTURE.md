# Architecture

```
 browser ──HTTP(S)──► Airdesk :8097 (FastAPI) ──TCP──► SpySwitch :5555/:5556 ──► SpyServer 127.0.0.1:15555/15556
    │                    │  └─ HTTP ──► tar1090 (aircraft.json, db)                 ▲
    └──HTTP(S)──► SpySwitch UI :8096 (FastAPI, same process as the proxy) ──────────┘
```

Both containers use host networking. SpySwitch takes over the SpyServer ports and proxies every client
connection upstream; its UI shows who is connected and lets the owner switch apps off (disconnect + refuse).

## Components
| Part | Role |
| --- | --- |
| `common/sdrcommon/webauth.py` | Sign-in for both apps: login/setup/logout/password API, session middleware, CSRF header rule, CSP and other headers |
| `common/sdrcommon/auth.py` | scrypt hashes in `users.json` (re-read when the CLI changes it), login throttle (10/IP, 5/account, 100 total per 15 min), server-side sessions bound to the password hash |
| `airdesk/radio.py` | `RadioEngine`: one worker thread holds the SpyServer connection, scans or holds a window, detects voice channels, records transmissions, feeds ACARS/VDL2 decoders |
| `airdesk/store.py` | SQLite (`airdesk.db`): channels, transmissions (μ-law files), listening coverage, scanner segments; retention by age and size |
| `airdesk/adsb.py` | Polls tar1090 for aircraft and trails |
| `airdesk/web.py` | REST API and `/ws` (state, aircraft, spectrum, messages, live audio of the channels a viewer listens to) |
| `spyswitch/switchboard.py` | asyncio TCP proxy, app registry (`apps.json`), command watcher for frequency/gain |

## Key decisions
- Sessions are server-side and in memory, keyed by SHA-256 of a 256-bit token; a password change invalidates them.
  Each app uses its own cookie name (`airdesk_session`, `spyswitch_session`) because they share a host name.
- First-run setup is only offered to private/loopback/link-local clients (`*_SETUP_FROM=any` overrides).
- Radio worker start/stop is serialised; `start()` ignores a worker that was told to stop but is still exiting.

## Threat model
| Threat | Mitigation | Gap |
| --- | --- | --- |
| Password guessing | scrypt (N=2^15), throttle per IP/account/global, dummy hash for unknown names, generic errors | no 2FA |
| CSRF | custom `X-App-Request` header + `Sec-Fetch-Site`, `SameSite=Strict` cookie | — |
| XSS / framing | strict CSP, `X-Frame-Options: DENY`, `nosniff`, `no-referrer` | — |
| Spoofed client IP | `X-Forwarded-For` only from `*_TRUSTED_PROXIES` (default: private ranges) | any LAN host counts as a trusted proxy by default |
| Eavesdropping | `Secure` cookie when reached over HTTPS | the apps serve plain HTTP; TLS needs a reverse proxy |
| Radio hogging | SpySwitch on/off per app | app identity (HELLO name + IP) is self-declared; the registry grows with every new name |
