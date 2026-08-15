"""The ``sentinel-agent`` Cloud Run service: one HTTP endpoint that runs the loop.

    POST /run[?trigger=scheduler]   execute one Sentinel run, return the report
    GET  /health                    liveness

Cloud Scheduler calls ``POST /run`` every SCAN_INTERVAL_MINUTES with an OIDC
token; the service is deployed *without* unauthenticated access, so only the
scheduler's service account (roles/run.invoker) can reach it. The run happens
inside the request (Cloud Run allocates CPU for the duration), which is why
the service is deployed with a long request timeout, concurrency 1 and a
single instance — one run at a time, and the Firestore-side overlap guard in
:mod:`sentinel.run` covers the rest.

    python -m sentinel.service        # local: http://localhost:8080/run
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from .config import Settings, settings as default_settings
from .run import RUN_FAILED, RUN_SKIPPED, SentinelRun

log = logging.getLogger(__name__)

Runner = Callable[[str], Awaitable[dict[str, Any]]]   # trigger -> report.as_dict()


def _default_runner(cfg: Settings) -> Runner:
    async def run(trigger: str) -> dict[str, Any]:
        from .firestore_client import get_db       # noqa: PLC0415 — only when actually serving
        from .repository import PigOpsRepository   # noqa: PLC0415

        repo = PigOpsRepository(get_db(), cfg.demo_farm_id)
        report = await SentinelRun(repo, cfg, trigger=trigger).execute_async()
        return report.as_dict()

    return run


def create_app(runner: Runner | None = None, cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or default_settings
    runner = runner or _default_runner(cfg)
    app = FastAPI(title="PigOps Sentinel — agent service", docs_url=None, redoc_url=None)
    lock = asyncio.Lock()

    @app.get("/")
    async def root() -> JSONResponse:
        return JSONResponse({"service": "pigops-sentinel-agent", "farm": cfg.demo_farm_id, "model": cfg.gemini_model,
                             "scanIntervalMinutes": cfg.scan_interval_minutes, "run": "POST /run"})

    @app.get("/health")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "busy": lock.locked()})

    @app.post("/run")
    async def run(trigger: str = Query(default="scheduler", max_length=40)) -> JSONResponse:
        if lock.locked():
            return JSONResponse({"status": "busy", "detail": "a run is already in progress on this instance"}, status_code=409)
        async with lock:
            log.info("run requested (trigger=%s)", trigger)
            report = await runner(trigger)
        # non-2xx only for a broken run so the Scheduler console shows it red; skipped is a normal outcome
        code = 500 if report.get("status") == RUN_FAILED else 200
        return JSONResponse(report, status_code=code)

    return app


def main(argv: list[str] | None = None) -> int:
    import uvicorn  # noqa: PLC0415

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
