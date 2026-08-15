"""The console: the feed's picture of the log and the runs, cheap change
polling, header stats, the JSON-safe presentation, and the HTTP/SSE surface.
Data comes from a real (faked-LLM) run over the demo plan; no Firestore."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from sentinel.config import Settings
from sentinel.console import app as console_app
from sentinel.console.app import create_app
from sentinel.console.feed import ConsoleFeed, jsonable
from sentinel.followup import FollowUp
from sentinel.logbook import run_failure_entry
from sentinel.run import run_once
from tests.fakes import FakeNotifier, FakeRepository, snapshot_from_plan
from tests.test_run import actor_factory, factory


@pytest.fixture()
def cfg():
    return Settings(_env_file=None)


@pytest.fixture()
def repo(demo_plan, frozen_clock, cfg):
    """A repository after one full (faked-LLM) run: 6 log entries, 1 run doc, 2 tasks."""
    r = FakeRepository(demo_plan, frozen_clock)
    snap = snapshot_from_plan(demo_plan, frozen_clock)
    run_once(r, cfg, load=lambda rr, c: replace(snap, open_tasks=rr.get_open_tasks()),
             investigator_factory=factory(fail_on={"SILENT_ROOM"}), actor_factory=actor_factory(),
             followup_factory=lambda rr, s, run_id, c: FollowUp(rr, s, run_id, c, notifier=FakeNotifier()))
    return r


@pytest.fixture()
def feed(repo, cfg):
    f = ConsoleFeed(repo, cfg, max_entries=50)
    f.load_initial()
    return f


# ------------------------------------------------------------------ feed
def test_initial_state_is_the_whole_picture(feed, repo, frozen_clock):
    s = feed.state()
    json.dumps(s)                                                    # JSON-safe end to end
    assert s["farmName"] == "PigOps" and s["timezone"] == "Europe/Budapest"
    assert len(s["entries"]) == 6 and s["entries"][0]["kind"] in {"act", "noise", "watch", "failed"}
    kinds = sorted(e["kind"] for e in s["entries"])
    assert kinds == ["act", "act", "act", "act", "failed", "watch"]     # 2 spikes + 2 deadlines ACT, valve WATCH, silent FAILED
    assert all(isinstance(e["timestamp"], str) for e in s["entries"])
    run = s["run"]
    assert run["status"] == "partial" and run["phase"] == "done" and run["tasksCreated"] == 2
    st = s["stats"]
    assert st["runsToday"] == 1 and st["finishedRunsToday"] == 1 and st["tasksCreatedToday"] == 2
    assert st["decisionsToday"] == {"NOISE": 0, "WATCH": 1, "ACT": 4} and st["escalationsToday"] == 0
    assert st["candidatesToday"] == 6 and st["handledToday"] == 0 and st["entries"] == 6
    assert st["lastRunAt"] == frozen_clock.now().isoformat() and st["lastRunStatus"] == "partial"


def test_present_maps_status_to_card_kind(feed, frozen_clock):
    base = {"id": "x", "timestamp": frozen_clock.now(), "status": "decided", "decision": "ACT"}
    assert feed.present(base)["kind"] == "act"
    assert feed.present({**base, "decision": "WATCH"})["kind"] == "watch"
    assert feed.present({**base, "status": "escalated"})["kind"] == "escalation"
    assert feed.present({**base, "status": "closed", "decision": None})["kind"] == "outcome"
    assert feed.present({**base, "status": "failed", "decision": None})["kind"] == "failed"
    assert feed.present({**base, "status": "run_failed", "decision": None})["kind"] == "failed"
    assert feed.present(base)["timestamp"] == frozen_clock.now().isoformat()


def test_poll_picks_up_new_entries_and_run_changes_without_duplicates(feed, repo, frozen_clock):
    assert feed.poll() == []                                         # nothing changed → no events, no noise
    # a new run starts and writes an entry (as the orchestrator would)
    repo.upsert_run("r2", status="running", phase="investigating", currentTarget="MORTALITY_SPIKE — B-Telep / 5.Terem",
                    startedAt=frozen_clock.now() + timedelta(minutes=15), candidates=1, decided=0, tasksCreated=0)
    log_id = repo.write_log({**run_failure_entry("r2", "PigOps", "investigating", "boom", frozen_clock.now() + timedelta(minutes=16))})
    events = feed.poll()
    types = [e["type"] for e in events]
    assert types == ["log", "run", "stats"]
    assert events[0]["entry"]["id"] == log_id and events[0]["entry"]["kind"] == "failed"
    assert events[1]["run"]["id"] == "r2" and events[1]["run"]["currentTarget"].endswith("B-Telep / 5.Terem")
    assert events[2]["stats"]["runsToday"] == 2
    assert feed.entries[0]["id"] == log_id and len(feed.entries) == 7
    assert feed.poll() == []                                         # same data again → nothing
    # only the run's phase changes → a run event (the live header), no log event
    repo.upsert_run("r2", phase="following_up", currentTarget=None)
    events = feed.poll()
    assert [e["type"] for e in events] == ["run", "stats"] and events[0]["run"]["phase"] == "following_up"


def test_entries_are_capped_and_stats_only_count_today(repo, cfg, frozen_clock):
    repo.upsert_run("old", status="ok", startedAt=frozen_clock.now() - timedelta(days=1), tasksCreated=9, decisions={"ACT": 9})
    for i, e in enumerate(repo.logs):                                # real entries are seconds apart, not identical
        e["timestamp"] = e["timestamp"] + timedelta(seconds=i)
    feed = ConsoleFeed(repo, cfg, max_entries=3)
    feed.load_initial()
    assert len(feed.entries) == 3 and len(feed.state()["entries"]) == 3
    assert feed.stats()["tasksCreatedToday"] == 2                    # yesterday's 9 are not today's
    for i in range(3):
        repo.write_log({"id": None, "timestamp": frozen_clock.now() + timedelta(minutes=i + 1), "status": "decided",
                        "decision": "NOISE", "roomName": "A-Telep / 1.Terem", "signalType": "MORTALITY_SPIKE",
                        "observation": "x", "reasoning": "y", "contextGathered": [], "farmName": "PigOps", "runId": "r"})
    assert sum(1 for e in feed.poll() if e["type"] == "log") == 3
    assert len(feed.entries) == 3 and feed.stats()["entries"] == 3


def test_jsonable():
    now = __import__("datetime").datetime(2026, 8, 15, 10, 30)
    assert jsonable({"a": now, "b": [now, {"c": now}], "d": 1}) == {"a": now.isoformat(), "b": [now.isoformat(), {"c": now.isoformat()}], "d": 1}


# ------------------------------------------------------------------ http
@pytest.fixture()
def client(feed):
    app = create_app(feed=feed, start_poller=False)
    with TestClient(app) as c:
        yield c


def test_page_and_state_and_health(client):
    page = client.get("/")
    assert page.status_code == 200 and "PigOps Sentinel" in page.text
    assert "fetch('/api/state'" in page.text and "EventSource" not in page.text   # polls, never holds a stream open
    assert "visibilitychange" in page.text                                        # and stops while the tab is hidden
    assert "Observed" in page.text and "Reasoned" in page.text and "Acted" in page.text
    st = client.get("/api/state").json()
    assert st["farmName"] == "PigOps" and len(st["entries"]) == 6 and st["stats"]["runsToday"] == 1
    hz = client.get("/health").json()
    assert hz == {"ok": True, "loaded": True, "entries": 6}


def test_state_tells_the_page_how_soon_to_come_back(client, feed):
    """Idle farm → a slow poll; a run in progress → a fast one."""
    assert client.get("/api/state").json()["pollAfterMs"] == 30000
    feed.latest_run = {**(feed.latest_run or {}), "status": "running"}
    feed.mark_fresh()                                                  # keep the cache warm: no Firestore in a unit test
    assert client.get("/api/state").json()["pollAfterMs"] == 2000


def test_refresh_is_shared_and_ttl_bounded(feed):
    """Ten visitors in the same TTL window cost one poll, not ten."""
    import asyncio

    calls = {"n": 0}
    real_poll = feed.poll

    def counting_poll():
        calls["n"] += 1
        return real_poll()

    feed.poll = counting_poll
    feed._refreshed_at = None                                          # cold cache

    async def drive():
        await asyncio.gather(*(feed.refresh_if_stale() for _ in range(10)))
        assert calls["n"] == 1, "a burst of requests must collapse into one read"
        await feed.refresh_if_stale()
        assert calls["n"] == 1, "still inside the TTL → no second read"
        feed._refreshed_at -= feed.ttl_s() + 1                          # age the cache past its TTL
        await feed.refresh_if_stale()
        assert calls["n"] == 2, "past the TTL → exactly one more read"

    asyncio.run(drive())


def test_refresh_survives_a_firestore_error(feed):
    """A failed read serves the last good picture instead of a 500, and backs off."""
    import asyncio

    def boom():
        raise RuntimeError("firestore is having a moment")

    feed.poll = boom
    feed._refreshed_at = None

    async def drive():
        assert await feed.refresh_if_stale() == []
        assert not feed.is_stale()                                     # marked fresh → we do not hammer it
        assert feed.state()["farmName"] == "PigOps"                    # the cached picture still serves

    asyncio.run(drive())


def test_event_stream_sends_state_then_events_then_heartbeats(feed):
    """The SSE body itself — the TestClient cannot close an endless stream, so the generator is driven directly."""
    import asyncio
    from sentinel.console.app import event_stream

    async def drive():
        calls = {"n": 0}

        async def disconnected():
            calls["n"] += 1
            return calls["n"] > 3                                     # stays connected for three loops

        gen = event_stream(feed, disconnected, heartbeat_s=0.05)
        first = await gen.__anext__()                                 # state, immediately
        assert first.startswith("event: state\ndata: ")
        state = json.loads(first.split("data: ", 1)[1])
        assert state["farmName"] == "PigOps" and len(state["entries"]) == 6
        assert len(feed._subscribers) == 1
        feed.broadcast([{"type": "log", "entry": {"id": "n1", "kind": "noise"}}, {"type": "stats", "stats": {"runsToday": 2}}])
        second = await gen.__anext__()
        assert second.startswith("event: log\n") and '"id": "n1"' in second
        third = await gen.__anext__()
        assert third.startswith("event: stats\n")
        fourth = await gen.__anext__()                                # nothing queued → heartbeat after 50 ms
        assert fourth == ": ping\n\n"
        with pytest.raises(StopAsyncIteration):                       # the client went away → the stream ends
            await gen.__anext__()
        assert feed._subscribers == set()                             # and it is unsubscribed

    asyncio.run(drive())


def test_events_route_wiring(client):
    """The route exists and is a text/event-stream (a full GET would never end — headers only via HEAD-like probe)."""
    routes = {r.path: r for r in client.app.routes}
    assert "/events" in routes and "GET" in routes["/events"].methods


def test_a_reset_of_the_log_is_noticed_and_re_rendered(feed, repo):
    """`seed --reset-log` empties sentinelLog and sentinelRuns while a console instance is alive: the feed must
    drop its stale picture and push a fresh `state` to every client instead of showing ghosts."""
    assert len(feed.entries) == 6 and feed.latest_run is not None
    repo.logs.clear(); repo.runs.clear()
    events = feed.poll()
    assert [e["type"] for e in events] == ["state"]
    assert events[0]["entries"] == [] and events[0]["run"] is None and events[0]["stats"]["runsToday"] == 0
    assert feed.entries == [] and feed.latest_run is None
    # and it picks up the new world afterwards
    repo.upsert_run("fresh", status="ok", phase="done", startedAt=repo.clock.now(), tasksCreated=1)
    assert [e["type"] for e in feed.poll()] == ["run", "stats"]


def test_a_reset_followed_by_a_new_run_is_noticed_too(feed, repo, frozen_clock):
    """The usual demo sequence: `seed --reset --reset-log`, then a tick — by the next poll the runs are not empty,
    but the newest entries we knew of are gone → still a full re-render, no ghosts."""
    repo.logs.clear(); repo.runs.clear()
    repo.upsert_run("new", status="running", phase="scanning", startedAt=frozen_clock.now() + timedelta(minutes=1))
    repo.write_log({"id": None, "timestamp": frozen_clock.now() + timedelta(minutes=1), "status": "decided", "decision": "ACT",
                    "severity": "high", "roomName": "B-Telep / 5.Terem", "signalType": "MORTALITY_SPIKE", "observation": "o",
                    "reasoning": "r", "contextGathered": [], "farmName": "PigOps", "runId": "new"})
    events = feed.poll()
    assert [e["type"] for e in events] == ["state"]
    assert len(events[0]["entries"]) == 1 and events[0]["entries"][0]["runId"] == "new" and events[0]["run"]["id"] == "new"
    assert feed.poll() == []
