# ruff: noqa: B008 - FastAPI dependencies are declared as argument defaults by design
"""The api application. Routes are thin: they check the session, call the reader or the command
publisher, and return JSON. Live events go out over one WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from desk.bus.redis_streams import RedisStreamsBus
from desk.core.clock import LiveClock
from desk.storage.db import Database
from desk_api.auth import COOKIE, LoginLimiter, Sessions, current_user, require_operator
from desk_api.commands import Commands
from desk_api.live import LiveFeed
from desk_api.settings import ApiSettings
from desk_api.state import StateReader
from desk_api.users import User, Users

log = logging.getLogger(__name__)

OPERATOR_COMMANDS = {"cycle", "plan", "kill", "resume", "brief"}  # "lines" is for every viewer
VIEWER_COMMANDS = {"status", "theses", "lines"}


class LoginBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class AskBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class CommandBody(BaseModel):
    command: str = Field(min_length=1, max_length=32)
    args: str = Field(default="", max_length=500)


def create_app(
    settings: ApiSettings | None = None,
    *,
    db: Database | None = None,
    live: LiveFeed | None = None,
    commands: Commands | None = None,
    run_live: bool = True,
) -> FastAPI:
    settings = settings or ApiSettings()
    owns_db = db is None
    db = db or Database(settings.database_url)
    sessions = Sessions(
        settings.session_secret.get_secret_value() if settings.session_secret else None,
        settings.session_hours,
    )
    live = live or LiveFeed(settings.redis_url)
    commands = commands or Commands(RedisStreamsBus(settings.redis_url, LiveClock()), LiveClock())
    limiter = LoginLimiter()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if owns_db:
            await db.open()
        users = Users(db.pool)
        await users.migrate()
        app.state.users = users
        app.state.reader = StateReader(
            db,
            heartbeat_minutes=settings.heartbeat_minutes,
            traded=set(settings.lab_markets) if settings.lab_enabled else None,
        )
        task = asyncio.create_task(live.run(), name="live-feed") if run_live else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if owns_db:
                await db.close()

    app = FastAPI(title="desk api", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.sessions = sessions
    app.state.live = live
    app.state.commands = commands
    app.state.settings = settings

    def reader(request: Request) -> StateReader:
        return request.app.state.reader

    # ---- session ----

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        key = request.client.host if request.client else "?"
        if limiter.blocked(key):
            raise HTTPException(
                status_code=429, detail="too many attempts; try again in a few minutes"
            )
        user = await request.app.state.users.verify(body.name, body.password)
        if user is None:
            limiter.failed(key)
            raise HTTPException(status_code=401, detail="wrong name or password")
        limiter.clear(key)
        response.set_cookie(
            COOKIE,
            sessions.issue(user),
            max_age=sessions.max_age,
            httponly=True,
            samesite="lax",
            secure=settings.cookie_secure,
            path="/",
        )
        return {"name": user.name, "role": user.role}

    @app.post("/api/logout")
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    async def me(user: User = Depends(current_user)) -> dict[str, str]:
        return {"name": user.name, "role": user.role}

    # ---- reads ----

    @app.get("/api/state")
    async def state(request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
        r = reader(request)
        book, health = await asyncio.gather(r.book(), r.health())
        return {"book": book, "health": health, "user": {"name": user.name, "role": user.role}}

    @app.get("/api/trades")
    async def trades(
        request: Request, limit: int = 30, user: User = Depends(current_user)
    ) -> list[dict]:
        return await reader(request).trades(limit=max(1, min(limit, 200)))

    @app.get("/api/cycles")
    async def cycles(
        request: Request, limit: int = 20, user: User = Depends(current_user)
    ) -> list[dict]:
        return await reader(request).cycles(limit=max(1, min(limit, 100)))

    @app.get("/api/journal")
    async def journal(
        request: Request, limit: int = 100, user: User = Depends(current_user)
    ) -> list[dict]:
        return await reader(request).journal(limit=max(1, min(limit, 500)))

    @app.get("/api/health")
    async def health(request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
        out = await reader(request).health()
        out["live_feed"] = {
            "connected": request.app.state.live.connected,
            "last_event_at": request.app.state.live.last_event_at,
            "clients": len(request.app.state.live.clients),
        }
        return out

    @app.get("/api/research/latest")
    async def research_latest(
        request: Request, hours: int = 24, user: User = Depends(current_user)
    ) -> list[dict]:
        return await reader(request).research_latest(hours=max(1, min(hours, 168)))

    @app.get("/api/research")
    async def research(
        request: Request,
        kind: str = "morning_note",
        limit: int = 1,
        user: User = Depends(current_user),
    ) -> list[dict]:
        if kind not in (
            "morning_note",
            "scorecard",
            "regime",
            "sector",
            "technical",
            "forecast",
            "news",
            "filing",
            "macro",
            "fundamental",
            "event",
            "playbook",
        ):
            raise HTTPException(status_code=400, detail="unknown research kind")
        return await reader(request).research(kind, limit=max(1, min(limit, 20)))

    @app.get("/api/brief")
    async def briefs(
        request: Request, limit: int = 5, user: User = Depends(current_user)
    ) -> list[dict]:
        return await reader(request).briefs(limit=max(1, min(limit, 30)))

    @app.get("/api/reviews")
    async def reviews(
        request: Request, limit: int = 20, user: User = Depends(current_user)
    ) -> list[dict]:
        return await reader(request).reviews(limit=max(1, min(limit, 100)))

    @app.get("/api/reviews/weekly")
    async def weekly_review(request: Request, user: User = Depends(current_user)) -> dict | None:
        return await reader(request).weekly_review()

    @app.get("/api/graduation")
    async def graduation_checklist(request: Request, user: User = Depends(current_user)) -> dict:
        return await reader(request).graduation()

    @app.get("/api/reviews/analysts")
    async def analyst_scores(request: Request, user: User = Depends(current_user)) -> dict | None:
        return await reader(request).analyst_scores()

    @app.get("/api/funnel")
    async def funnel(
        request: Request,
        market: str = "stocks",
        at: str | None = None,
        user: User = Depends(current_user),
    ) -> dict:
        try:
            return await reader(request).funnel(market, at)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail="unknown market") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="bad time") from exc

    @app.get("/api/funnel/detail")
    async def funnel_detail(
        request: Request,
        symbol: str,
        market: str = "stocks",
        at: str | None = None,
        user: User = Depends(current_user),
    ) -> dict:
        try:
            return await reader(request).funnel_detail(market, symbol.upper(), at)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail="unknown market or symbol") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="bad time") from exc

    @app.get("/api/funnel/markets")
    async def funnel_markets(request: Request, user: User = Depends(current_user)) -> list[dict]:
        return await reader(request).funnel_markets()

    @app.get("/api/bars")
    async def bars(
        request: Request,
        symbol: str,
        interval: str = "1d",
        n: int = 60,
        user: User = Depends(current_user),
    ) -> list[dict]:
        if interval not in ("1m", "1d"):
            raise HTTPException(status_code=400, detail="interval must be 1m or 1d")
        return await reader(request).bars(symbol, interval, max(1, min(n, 2000)))

    @app.get("/api/chat/history")
    async def chat_history(
        request: Request, limit: int = 30, user: User = Depends(current_user)
    ) -> list[dict]:
        return await reader(request).chat(limit=max(1, min(limit, 200)))

    @app.get("/api/chat/{request_id}")
    async def chat_answer(
        request: Request, request_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        out = await reader(request).answer(request_id)
        return out or {"request_id": request_id, "answer": None, "at": None}

    @app.get("/api/commands/{request_id}")
    async def command_result(
        request: Request, request_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        out = await reader(request).command_result(request_id)
        return out or {"request_id": request_id, "ok": None, "text": None}

    async def require_bot(request: Request) -> None:
        health = await reader(request).health()
        if not health["bot"]["alive"]:
            raise HTTPException(
                status_code=503,
                detail="the bot is not running right now, so nobody can answer; try again once it is back",
            )

    @app.get("/api/agents")
    async def agents(request: Request, user: User = Depends(current_user)) -> list[dict]:
        return await reader(request).agents()

    # ---- requests to the bot ----

    @app.post("/api/ask")
    async def ask(
        body: AskBody, request: Request, user: User = Depends(current_user)
    ) -> dict[str, str]:
        await require_bot(request)
        request_id = await request.app.state.commands.ask(body.text.strip(), user)
        return {"request_id": request_id}

    @app.post("/api/commands")
    async def command(
        body: CommandBody, request: Request, user: User = Depends(current_user)
    ) -> dict[str, str]:
        name = body.command.strip().lower()
        if name in OPERATOR_COMMANDS:
            await require_operator(request)
        elif name not in VIEWER_COMMANDS:
            raise HTTPException(status_code=400, detail=f"unknown command {name}")
        await require_bot(request)
        request_id = await request.app.state.commands.run(name, body.args.strip(), user)
        return {"request_id": request_id}

    # ---- live ----

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        user = sessions.read(websocket.cookies.get(COOKIE))
        if user is None:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        feed: LiveFeed = websocket.app.state.live
        q = feed.subscribe()
        try:
            await websocket.send_json(
                {
                    "type": "hello",
                    "at": datetime.now(UTC).isoformat(),
                    "user": {"name": user.name, "role": user.role},
                    "feed_connected": feed.connected,
                }
            )
            while True:
                message = await q.get()
                await websocket.send_json(message)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001 - a broken socket is the client's problem, log and move on
            log.info("websocket closed: %s", exc)
        finally:
            feed.unsubscribe(q)

    dist = Path(settings.web_dist)
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="web")

    return app
