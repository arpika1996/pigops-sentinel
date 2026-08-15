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

Why the page polls instead of holding an SSE stream open
--------------------------------------------------------
On Cloud Run an open SSE connection is an open *request*, and a request keeps
an instance alive and billable for as long as it lasts — a forgotten browser
tab cost us an instance-hour at a time (50 requests in one day held ~5 hours
of instance time). The page therefore polls ``/api/state``: every 30 s while
the farm is idle, every 2 s while a run is in progress, and not at all while
the tab is hidden. Each poll is a sub-second request, and the feed only
touches Firestore when its own cache went stale, so more visitors do not mean
more reads. ``/events`` still works for anyone who wants a stream — it drives
the same cached refresh — but nothing in the page uses it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
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
MAX_STREAM_S = 240.0        # an SSE stream never pins an instance longer than this
IDLE_POLL_S = 30.0          # how often the page re-asks while the farm is idle


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def event_stream(feed: ConsoleFeed, is_disconnected, heartbeat_s: float | None = None,
                       max_seconds: float = MAX_STREAM_S) -> AsyncIterator[str]:
    """The SSE body: the full state first, then every broadcast event; a comment
    line as heartbeat when nothing happens, until the client goes away.

    Bounded by ``max_seconds`` so a forgotten stream cannot pin an instance for
    the whole Cloud Run request timeout, and it drives ``refresh_if_stale``
    itself, so it works without a background poller.
    """
    queue = feed.subscribe()
    deadline = time.monotonic() + max_seconds
    try:
        await feed.refresh_if_stale()
        yield _sse("state", feed.state())
        while not await is_disconnected():
            if time.monotonic() >= deadline:
                yield _sse("bye", {"reason": "stream time limit"})
                return
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=heartbeat_s or HEARTBEAT_S)
            except asyncio.TimeoutError:
                for fresh in await feed.refresh_if_stale():       # nobody polls in the background
                    yield _sse(fresh.get("type", "message"), fresh)
                yield ": ping\n\n"
                continue
            yield _sse(ev.get("type", "message"), ev)
    finally:
        feed.unsubscribe(queue)


def create_app(repo: Any = None, cfg: Settings | None = None, *, feed: ConsoleFeed | None = None,
               poll_interval_s: float = 2.0, start_poller: bool = False) -> FastAPI:
    """Build the app. ``repo`` defaults to the real Firestore repository; tests pass a fake.

    ``start_poller`` is off by default: a background poll reads Firestore even
    when nobody is watching. Requests refresh the feed on demand instead.
    """
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
        await feed.refresh_if_stale()          # one shared read per TTL, however many visitors
        payload = feed.state()
        running = bool(feed.latest_run and feed.latest_run.get("status") == "running")
        payload["pollAfterMs"] = int((feed.ttl_running_s if running else IDLE_POLL_S) * 1000)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.get("/health")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "loaded": feed.loaded, "entries": len(feed.entries)})

    @app.get("/events")
    async def events(request: Request) -> StreamingResponse:
        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
        return StreamingResponse(event_stream(feed, request.is_disconnected), media_type="text/event-stream", headers=headers)

    return app


