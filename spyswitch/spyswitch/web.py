"""SpySwitch web UI and API."""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from sdrcommon.webauth import install_auth

from . import __version__
from .switchboard import Registry, Switchboard, parse_servers

STATIC = Path(__file__).parent / "static"
DEFAULT_SERVERS = "VHF/UHF@5555=127.0.0.1:15555; HF@5556=127.0.0.1:15556"


class Allowed(BaseModel):
    key: str = Field(max_length=200)
    allowed: bool


class AppKey(BaseModel):
    key: str = Field(max_length=200)


class Defaults(BaseModel):
    default_allow: bool


def create_app(board: Switchboard | None = None, **auth_kwargs) -> FastAPI:
    env = os.environ.get
    if board is None:
        data = Path(env("SPYSWITCH_DATA", "/data"))
        board = Switchboard(parse_servers(env("SPYSWITCH_SERVERS", DEFAULT_SERVERS)),
                            Registry(data / "apps.json", env("SPYSWITCH_DEFAULT_ALLOW", "true").lower() == "true"),
                            env("SPYSWITCH_LISTEN", "0.0.0.0"))

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await board.start()
        yield
        await board.stop()

    app = FastAPI(title="SpySwitch", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.board = board
    auth = install_auth(app, app_name="SpySwitch", prefix="SPYSWITCH", **auth_kwargs)
    app.state.auth = auth

    # endpoints are async so they run on the event loop that owns the proxy connections
    def act(fn, *args):
        try:
            fn(*args)
        except KeyError as e:
            raise HTTPException(404, "That app or connection no longer exists.") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return board.snapshot()

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    async def state():
        return board.snapshot()

    @app.post("/api/apps/allowed")
    async def allowed(body: Allowed):
        return act(board.set_allowed, body.key, body.allowed)

    @app.post("/api/apps/forget")
    async def forget(body: AppKey):
        return act(board.forget, body.key)

    @app.post("/api/connections/{conn_id}/disconnect")
    async def disconnect(conn_id: int):
        return act(board.disconnect, conn_id)

    @app.post("/api/settings")
    async def settings(body: Defaults):
        return act(board.set_default_allow, body.default_allow)

    @app.websocket("/ws")
    async def live(ws: WebSocket):
        if auth.ws_user(ws) is None:
            await ws.close(code=4401)
            return
        token = auth.ws_token(ws)
        await ws.accept()
        try:
            while True:
                if auth.sessions.user(token) is None:
                    await ws.close(code=4401)
                    return
                await ws.send_json(board.snapshot())
                await asyncio.sleep(0.5)
        except (WebSocketDisconnect, RuntimeError):
            pass

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
