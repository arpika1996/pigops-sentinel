"""Sentinel Console — one page, read-only, English (SPEC §6).

    python -m sentinel.console            # http://localhost:8081

Routes
------
GET /            the page (static/index.html, everything inline)
GET /api/state   the full picture: header stats, latest run, last entries
GET /events      Server-Sent Events: `state` once, then `log` / `run` / `stats`
GET /health     liveness for Cloud Run

The app never writes: it holds a :class:`~sentinel.console.feed.ConsoleFeed`
that only calls the repository's ``get_*`` methods (and, on Cloud Run, runs
as the ``sentinel-console`` service account which only has datastore.viewer).
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from ..config import Settings, settings as default_settings
from .feed import ConsoleFeed

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
HEARTBEAT_S = 15.0


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def event_stream(feed: ConsoleFeed, is_disconnected, heartbeat_s: float | None = None) -> AsyncIterator[str]:
    """The SSE body: the full state first, then every broadcast event; a comment
    line as heartbeat when nothing happens, until the client goes away."""
    queue = feed.subscribe()
    try:
        yield _sse("state", feed.state())
        while not await is_disconnected():
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=heartbeat_s or HEARTBEAT_S)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            yield _sse(ev.get("type", "message"), ev)
    finally:
        feed.unsubscribe(queue)


def create_app(repo: Any = None, cfg: Settings | None = None, *, feed: ConsoleFeed | None = None,
               poll_interval_s: float = 2.0, start_poller: bool = True) -> FastAPI:
    """Build the app. ``repo`` defaults to the real Firestore repository; tests pass a fake."""
    cfg = cfg or default_settings

    if feed is None:
        if repo is None:
            from ..firestore_client import get_db      # noqa: PLC0415 — only when actually serving
            from ..repository import PigOpsRepository  # noqa: PLC0415

            repo = PigOpsRepository(get_db(), cfg.demo_farm_id)
        feed = ConsoleFeed(repo, cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.feed = feed
        try:
            await asyncio.to_thread(feed.load_initial)
        except Exception:  # noqa: BLE001 — serve the page anyway; the poller retries
            log.exception("initial load failed; the console starts empty")
        task = asyncio.create_task(feed.run_forever(poll_interval_s)) if start_poller else None
        app.state.poller = task
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    app = FastAPI(title="PigOps Sentinel Console", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.feed = feed

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/state")
    async def state() -> JSONResponse:
        return JSONResponse(feed.state())

    @app.get("/health")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "loaded": feed.loaded, "entries": len(feed.entries)})

    @app.get("/events")
    async def events(request: Request) -> StreamingResponse:
        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
        return StreamingResponse(event_stream(feed, request.is_disconnected), media_type="text/event-stream", headers=headers)

    return app


