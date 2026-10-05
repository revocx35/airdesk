# Airdesk

Two small web apps for a home SDR setup.

- **Airdesk** shows the planes your ADS-B receiver sees on a map, and next to it what they say: airband voice (live and recorded) and their data messages (ACARS and VDL2), linked to the plane on the map.
- **SpySwitch** sits in front of your SpyServers and shows which app is using which one. Every app has an on/off switch: switch one off and it is disconnected and refused, so another app can have the radio.

Both are Python, run in Docker, and need a sign-in.

## What Airdesk uses

- **Plane data** from an existing [readsb](https://github.com/wiedehopf/readsb)/[tar1090](https://github.com/wiedehopf/tar1090) installation, read over HTTP (`/data/aircraft.json`). Nothing is changed there. Registration and type come from tar1090's own aircraft database files.
- **Radio** from a SpyServer, normally through SpySwitch. An RTL-SDR covers about 2 MHz at a time, so Airdesk works on a *window*: every channel inside it is decoded at once.
  - Voice channels are AM-demodulated with a squelch that follows the noise floor. Listen live in the browser, and replay recent transmissions.
  - **ACARS** (131.525/131.725/131.825 MHz) is decoded by Airdesk itself.
  - **VDL2** (136.725–136.975 MHz) is decoded by [dumpvdl2](https://github.com/szpajder/dumpvdl2), which is built into the image.
  - *Heard recently* lists frequencies in the window that carried signals, so you can find the active channels near you and add them.

Presets: **VDL2** (window around 136.4 MHz: VDL2 plus upper airband voice) and **ACARS** (around 131.2 MHz: ACARS plus 130–132 MHz voice). You can also type any centre frequency.

## Set up

1. **Move the SpyServers out of the way.** SpySwitch takes over their ports, so let each SpyServer listen only on localhost on another port. In `spyserver.config`:
   ```
   bind_host = 127.0.0.1
   bind_port = 15555
   ```
   and `15556` for a second instance (for example an HF one on 5556). Restart them.
2. **Configure.** Copy `.env.example` to `.env` and set `AIRDESK_READSB_URL` to your tar1090, e.g. `http://192.168.1.20/tar1090`.
3. **Start** on the machine that runs the SpyServers:
   ```sh
   docker compose up -d            # or: docker compose up -d --build
   ```
4. **Create accounts** (passwords need 10+ characters):
   ```sh
   docker compose exec airdesk python -m sdrcommon.users add NAME
   docker compose exec spyswitch python -m sdrcommon.users add NAME
   ```
5. Open Airdesk on port **8097** and SpySwitch on **8096**. Clients like SDR# and SDR++ keep using ports 5555/5556 as before.

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `AIRDESK_READSB_URL` | – | tar1090 base URL; Airdesk reads `<url>/data/aircraft.json` |
| `AIRDESK_SPYSERVER_HOST` / `_PORT` | `127.0.0.1` / `5555` | Where Airdesk gets its radio (SpySwitch) |
| `AIRDESK_RECEIVER_LAT` / `_LON` | – | Your antenna, for distances and the starting map view |
| `SPYSWITCH_SERVERS` | `VHF/UHF@5555=127.0.0.1:15555; HF@5556=127.0.0.1:15556` | `NAME@LISTEN_PORT=SPYSERVER_HOST:PORT`, separated by `;` |
| `SPYSWITCH_DEFAULT_ALLOW` | `true` | Whether an app SpySwitch has never seen may connect |
| `AIRDESK_TILE_URL` / `_ATTRIBUTION` | OpenStreetMap | Map tiles (a Leaflet URL template) and their credit line |
| `AIRDESK_TILE_DARK_FILTER` | `true` | Darken light tiles when the browser is in dark mode |
| `*_HTTP_PORT` | `8097` / `8096` | Web ports |
| `*_TRUSTED_PROXIES` | `private` | Proxies whose `X-Forwarded-For`/`-Proto` are believed: `private`, `none`, or IPs/CIDRs |
| `*_SECURE_COOKIES` | `auto` | Secure cookie when reached over HTTPS; `true`/`false` to force |
| `*_ADMIN_USER` / `*_ADMIN_PASSWORD` | – | Optional first account, created at start-up if missing |

## SpySwitch

SpyServer clients send their name when they connect (`SDR#`, `SDR++`, `airdesk`, `atvrx`, ...). SpySwitch keys apps by that name plus their address, so two apps on the same machine are told apart. For each server it shows the app connected, its tuned frequency and gain, and how much data it is receiving.

- **Switch an app off**: it is disconnected at once and refused until switched back on.
- **Disconnect**: drop it once; it may reconnect.
- **Let new apps connect**: turn off to refuse apps SpySwitch has not seen before until you allow them.

The switches organise who gets the radio; they are not a security boundary, since a client could send another app's name.

## Accounts and security

Accounts are stored as scrypt hashes in each app's data volume. Failed sign-ins are limited per address (10), per account (5) and overall (100) per 15 minutes. Sessions are HttpOnly, SameSite=Strict cookies; API writes need an `X-App-Request` header and are refused when the browser marks them cross-site, WebSockets check their Origin, and pages are served with a strict Content-Security-Policy.

## Tests

```sh
docker build -f airdesk/Dockerfile --target test .
docker build -f spyswitch/Dockerfile --target test .
```

The suites use a fake SpyServer that plays synthetic airband scenes (AM voice, ACARS bursts) in real time, a fake tar1090, and the real dumpvdl2 inside the Airdesk image. The ACARS decoder was developed against real off-air bursts, which are not included.

## Notes

Receiving airband traffic is legal in many places but not everywhere, and recording or sharing it often is not. Check your local rules. Airdesk keeps recent transmissions in memory only; turn that off under *Transmissions*.

Third-party code: [Leaflet](https://leafletjs.com) 1.9.4 (BSD-2-Clause, bundled under `airdesk/airdesk/static/vendor/leaflet`), and in the Airdesk image [dumpvdl2](https://github.com/szpajder/dumpvdl2) v2.7.0 (GPL-3.0) with [libacars](https://github.com/szpajder/libacars) v2.2.1 (MIT), built from those tags (see `airdesk/Dockerfile`). Map tiles © OpenStreetMap contributors by default; heavy use should go to your own or a commercial tile server ([OSM tile policy](https://operations.osmfoundation.org/policies/tiles/)).
