"""The agent's HTTP surface (what Cloud Scheduler calls): one run per request,
report as JSON, 500 only for a broken run, busy → 409. The run itself is faked."""

from __future__ import annotations

from fastapi.testclient import TestClient

from sentinel.config import Settings
from sentinel.service import create_app


def _client(status="ok"):
    calls = []

    async def runner(trigger: str):
        calls.append(trigger)
        return {"runId": "20260815-120000-abc123", "status": status, "trigger": trigger, "candidates": 2, "decided": 2}

    return TestClient(create_app(runner, Settings(_env_file=None))), calls


def test_root_and_health():
    c, _ = _client()
    assert c.get("/").json()["run"] == "POST /run"
    assert c.get("/health").json() == {"ok": True, "busy": False}


def test_run_returns_the_report():
    c, calls = _client()
    r = c.post("/run?trigger=scheduler")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["trigger"] == "scheduler"
    assert calls == ["scheduler"]
    assert c.post("/run").json()["trigger"] == "scheduler"           # default trigger


def test_failed_run_is_a_500_skipped_is_not():
    c, _ = _client("failed")
    assert c.post("/run").status_code == 500
    c, _ = _client("skipped")
    assert c.post("/run").status_code == 200
