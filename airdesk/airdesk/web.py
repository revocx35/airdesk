"""Airdesk web app: map, radio, messages."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from sdrcommon.webauth import install_auth

from . import __version__
from .adsb import AdsbFeed
from .messages import MessageLog
from .radio import ConfigStore, RadioConfig, RadioEngine
from .store import Store

STATIC = Path(__file__).parent / "static"
DEFAULT_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
DEFAULT_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'


def tile_origin(template: str) -> str:
    """CSP source for a tile URL template like https://{s}.tile.example/{z}/{x}/{y}.png"""
    from urllib.parse import urlsplit
    u = urlsplit(template.replace("{s}", "a"))
    host = u.netloc.replace("a.", "*.", 1) if "{s}" in template else u.netloc
    return f"{u.scheme}://{host}"
PRESETS = {
    "vdl2": {"label": "VDL2 data + upper airband voice", "center_hz": 136_400_000.0},
    "acars": {"label": "ACARS data + 130-132 MHz voice", "center_hz": 131_200_000.0},
}


class RadioSettings(BaseModel):
    center_mhz: float | None = Field(None, ge=24, le=1800)
    gain: int | None = Field(None, ge=0, le=100)
    host: str | None = Field(None, min_length=1, max_length=255)
    port: int | None = Field(None, ge=1, le=65535)
    vdl2: bool | None = None
    acars: bool | None = None
    keep_clips: bool | None = None


class ChannelIn(BaseModel):
    freq_mhz: float = Field(ge=24, le=1800)
    label: str = Field("", max_length=40)
    squelch_db: float = Field(8.0, ge=3, le=30)


class ChannelEdit(BaseModel):
    label: str | None = Field(None, max_length=40)
    squelch_db: float | None = Field(None, ge=3, le=30)
    pinned: bool | None = None


class Mode(BaseModel):
    mode: str = Field(pattern="^(scan|fixed)$")


class Preset(BaseModel):
    name: str


def create_app(feed: AdsbFeed | None = None, engine: RadioEngine | None = None, messages: MessageLog | None = None,
               start_background: bool = True, **auth_kwargs) -> FastAPI:
    env = os.environ.get
    data = Path(env("AIRDESK_DATA", "/data"))
    if feed is None:
        rx = None
        if env("AIRDESK_RECEIVER_LAT") and env("AIRDESK_RECEIVER_LON"):
            rx = (float(env("AIRDESK_RECEIVER_LAT")), float(env("AIRDESK_RECEIVER_LON")))
        feed = AdsbFeed(env("AIRDESK_READSB_URL", "http://127.0.0.1/tar1090"), receiver=rx)
    messages = messages or MessageLog(feed.find, path=data / "messages.jsonl")
    if engine is None:
        defaults = RadioConfig(host=env("AIRDESK_SPYSERVER_HOST", "127.0.0.1"),
                               port=int(env("AIRDESK_SPYSERVER_PORT", "5555")))
        recordings = Store(data, retention_days=float(env("AIRDESK_RECORDING_DAYS", "7")),
                           max_mb=float(env("AIRDESK_RECORDING_MAX_MB", "4000")))
        engine = RadioEngine(ConfigStore(data / "radio.json", defaults), messages, recordings)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        if start_background:
            feed.start()
            if engine.cfg.running:
                engine.start()
        yield
        feed.stop()
        await asyncio.to_thread(engine.shutdown)

    app = FastAPI(title="Airdesk", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.feed, app.state.engine, app.state.messages = feed, engine, messages
    tiles = {"url": env("AIRDESK_TILE_URL", DEFAULT_TILES),
             "attribution": env("AIRDESK_TILE_ATTRIBUTION", DEFAULT_ATTRIBUTION),
             "dark_filter": env("AIRDESK_TILE_DARK_FILTER", "true").lower() == "true"}
    auth = install_auth(app, app_name="Airdesk", prefix="AIRDESK", extra_csp={"img-src": tile_origin(tiles["url"])},
                        **auth_kwargs)
    app.state.auth = auth

    def state() -> dict:
        return {"adsb": feed.status_snapshot(), "radio": engine.snapshot(),
                "messages": dict(messages.counts),
                "presets": {k: v["label"] for k, v in PRESETS.items()}, "tiles": tiles}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    def get_state():
        return state()

    @app.get("/api/aircraft")
    def aircraft():
        return {"aircraft": feed.snapshot(), "trails": feed.trails_snapshot()}

    @app.get("/api/messages")
    def get_messages(aircraft: str = "", limit: int = 200):
        return messages.list(aircraft, min(max(limit, 1), 1000))

    def channel_rows(status: str = "all") -> list[dict]:
        now = time.time()
        starts = engine.rec.recent_starts(now - 86400)
        rows = []
        for c in engine.channels():
            if status != "all" and c["status"] != status:
                continue
            hours = [0] * 24
            for t in starts.get(c["id"], []):
                hours[min(23, int((now - t) // 3600))] += 1
            rows.append({**c, "last_24h": hours[::-1], "tx_24h": sum(hours)})
        return rows

    def label_of(cid: str) -> str:
        c = engine.rec.channel(cid)
        return (c["label"] or f"{c['freq_hz'] / 1e6:.3f}") if c else cid

    @app.get("/api/channels")
    def list_channels(status: str = "all"):
        return channel_rows(status)

    @app.get("/api/channels/{cid}/history")
    def channel_history(cid: str, hours: int = 24):
        c = engine.rec.channel(cid)
        if c is None:
            raise HTTPException(404, "No such channel")
        hours = min(max(hours, 1), 24 * 7)
        since = time.time() - hours * 3600
        coverage = engine.rec.coverage(c["freq_hz"], since)
        now, cur = time.time(), engine.current_dwell()
        if cur and cur[1] <= c["freq_hz"] <= cur[2]:              # listening to it right now
            coverage.append((cur[0], now))
        return {"channel": c, "since": since, "until": now,
                "transmissions": engine.rec.transmissions(cid, since, 5000), "coverage": coverage}

    @app.get("/api/recordings")
    def recordings(limit: int = 150):
        rows = engine.rec.transmissions(None, 0, min(max(limit, 1), 500))
        return [{**r, "label": label_of(r["channel_id"])} for r in rows]

    @app.get("/api/recordings/{tid}.wav")
    def recording_wav(tid: int):
        data = engine.rec.wav(tid)
        if data is None:
            raise HTTPException(404, "That recording is no longer kept.")
        return Response(data, media_type="audio/wav", headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/radio/mode")
    def radio_mode(body: Mode):
        engine.update(mode=body.mode)
        return state()

    @app.post("/api/radio/start")
    def radio_start():
        engine.start()
        return state()

    @app.post("/api/radio/stop")
    def radio_stop():
        engine.stop()
        return state()

    @app.post("/api/radio/settings")
    def radio_settings(body: RadioSettings):
        ch = body.model_dump(exclude_none=True)
        if "center_mhz" in ch:
            ch["center_hz"] = ch.pop("center_mhz") * 1e6
        engine.update(**ch)
        return state()

    @app.post("/api/radio/preset")
    def radio_preset(body: Preset):
        if body.name not in PRESETS:
            raise HTTPException(400, "Unknown preset")
        engine.update(center_hz=PRESETS[body.name]["center_hz"])
        return state()

    def changed(fn, *args, **kw):
        try:
            fn(*args, **kw)
        except KeyError as e:
            raise HTTPException(404, "No such channel") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return state()

    @app.post("/api/channels")
    def add_channel(body: ChannelIn):
        return changed(engine.add_channel, round(body.freq_mhz * 1e6), body.label.strip(), body.squelch_db)

    @app.post("/api/channels/{cid}")
    def edit_channel(cid: str, body: ChannelEdit):
        fields = body.model_dump(exclude_none=True)
        if "label" in fields:
            fields["label"] = fields["label"].strip()
        return changed(engine.update_channel, cid, **fields)

    @app.delete("/api/channels/{cid}")
    def delete_channel(cid: str):
        return changed(engine.delete_channel, cid)

    @app.websocket("/ws")
    async def live(ws: WebSocket):
        if auth.ws_user(ws) is None:
            await ws.close(code=4401)
            return
        token = auth.ws_token(ws)
        await ws.accept()
        loop = asyncio.get_running_loop()
        events: asyncio.Queue = asyncio.Queue(maxsize=500)

        def push(kind, item):
            def put():
                if not events.full():
                    events.put_nowait((kind, item))
            loop.call_soon_threadsafe(put)

        on_msg = lambda m: push("message", m)
        on_tx = lambda t: push("transmission", {**t, "label": label_of(t["channel_id"])})
        messages.subscribe(on_msg)
        engine.on_transmission(on_tx)
        listen: set[str] = set()
        viewer = id(ws)
        await ws.send_text(json.dumps({"type": "hello", "state": state(), "aircraft": feed.snapshot(),
                                       "trails": feed.trails_snapshot(), "messages": messages.list(limit=150),
                                       "transmissions": recordings(150)}))

        async def receiver():
            nonlocal listen
            while True:
                data = await ws.receive_json()
                if isinstance(data, dict) and isinstance(data.get("listen"), list):
                    listen = {str(x) for x in data["listen"][:20]}
                    engine.set_listening(viewer, listen)

        rx = asyncio.create_task(receiver())
        seq = engine.audio_seq
        t_air = t_state = t_spec = 0.0
        try:
            while not rx.done():
                now = time.monotonic()
                if now - t_state >= 0.25:
                    if auth.sessions.user(token) is None:
                        await ws.close(code=4401)
                        return
                    await ws.send_text(json.dumps({"type": "state", "state": state()}))
                    t_state = now
                if now - t_spec >= 0.5 and engine.spectrum:
                    await ws.send_text(json.dumps({"type": "spectrum", "spectrum": engine.spectrum}))
                    t_spec = now
                if now - t_air >= 1.0:
                    await ws.send_text(json.dumps({"type": "aircraft", "aircraft": feed.snapshot()}))
                    t_air = now
                while not events.empty():
                    kind, item = events.get_nowait()
                    await ws.send_text(json.dumps({"type": kind, kind: item}))
                frames = engine.audio_since(seq)
                if frames:
                    seq = frames[-1][0]
                    if listen:
                        parts = []
                        for _, f in frames[-10:]:                    # never more than 0.5 s behind
                            chans = [f[c] for c in listen if c in f and len(f[c])]
                            if chans:
                                parts.append(np.sum(np.stack(chans).astype(np.int32), axis=0))
                        if parts:
                            pcm = np.clip(np.concatenate(parts), -32767, 32767).astype("<i2")
                            await ws.send_bytes(b"\x01" + pcm.tobytes())
                await asyncio.sleep(0.04)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            rx.cancel()
            messages.unsubscribe(on_msg)
            engine.off_transmission(on_tx)
            engine.set_listening(viewer, set())

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
