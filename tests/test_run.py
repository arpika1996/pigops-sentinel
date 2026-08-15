"""The run orchestrator: every candidate is logged the moment it is decided
(and acted on), the run doc tracks phase and counters, overlapping runs are
refused, failures end in the log, and what was handled once is not
investigated again. The investigator and FCM are faked — no LLM, no Firestore."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import timedelta

import pytest

from sentinel.actions import Actor
from sentinel.agent.investigator import InvestigationResult
from sentinel.agent.schemas import ContextItem, Decision
from sentinel.agent.tools import ToolCall
from sentinel.config import Settings
from sentinel.logbook import SIGNAL_RUN, STATUS_DECIDED, STATUS_FAILED, STATUS_RUN_FAILED
from sentinel.run import (
    PHASE_ACTING, PHASE_DONE, PHASE_INVESTIGATING, PHASE_SCANNING,
    RUN_FAILED, RUN_OK, RUN_PARTIAL, RUN_RUNNING, RUN_SKIPPED,
    SentinelRun, run_once,
)
from sentinel.scanner import scan
from tests.fakes import FakeNotifier, FakeRepository, snapshot_from_plan

ID_PATTERNS = [r"\bh[12]-t\d", r"-v\d+\b", r"demo-farm", r"\btask-[a-z]", r"demo-(admin|worker)"]


# --------------------------------------------------------------------------
# A scripted investigator: decides by signal type, can fail or crash on demand
# --------------------------------------------------------------------------
class FakeInvestigator:
    created: list["FakeInvestigator"] = []

    def __init__(self, repo, snapshot, run_id, cfg, status, *, fail_on=(), crash_on=None):
        self.repo, self.snapshot, self.run_id, self.cfg, self.status = repo, snapshot, run_id, cfg, status
        self.fail_on, self.crash_on = set(fail_on), crash_on
        self.seen: list[str] = []
        self.closed = False
        FakeInvestigator.created.append(self)

    async def investigate(self, c) -> InvestigationResult:
        self.seen.append(c.key)
        self.status("investigating", c.room_name)
        if c.signal_type == self.crash_on:
            raise RuntimeError("Vertex exploded")
        trace = [ToolCall("get_room_stats", {"room": c.room_name}, True, 5, "ok"),
                 ToolCall("get_mortality_history", {"room": c.room_name, "days": 14}, True, 7, "ok")]
        base = dict(candidate=c, raw_output="{}", tool_trace=trace, latency_ms=100, prompt_tokens=50, output_tokens=10,
                    total_tokens=60, llm_calls=2, model="fake")
        if c.signal_type in self.fail_on:
            return InvestigationResult(decision=None, error="ValueError: decision did not validate", **base)
        if c.signal_type == "MORTALITY_SPIKE":
            d = Decision(decision="ACT", severity="high", reasoning="Spike well above baseline.", action_title="Check the pigs",
                         action_body="Vet today.", context_gathered=[ContextItem(tool="get_mortality_history", why="baseline", finding="mean 0.7")])
        elif c.signal_type == "DEADLINE_RISK":
            d = Decision(decision="ACT", severity="medium", reasoning="Overdue and untouched.", action_title="Reminder", action_body="Do it.")
        elif c.signal_type == "VALVE_INSTABILITY":
            d = Decision(decision="WATCH", reasoning="Net zero edits.", watch_reason="More flapping tomorrow.")
        else:
            d = Decision(decision="NOISE", reasoning="Nothing to do.")
        return InvestigationResult(decision=d, **base)

    async def close(self) -> None:
        self.closed = True


def factory(**kw):
    return lambda repo, snap, run_id, cfg, status: FakeInvestigator(repo, snap, run_id, cfg, status, **kw)


@pytest.fixture()
def cfg():
    return Settings(_env_file=None)


@pytest.fixture()
def repo(demo_plan, frozen_clock):
    FakeInvestigator.created.clear()
    return FakeRepository(demo_plan, frozen_clock)


@pytest.fixture()
def snapshot(demo_plan, frozen_clock):
    return snapshot_from_plan(demo_plan, frozen_clock)


@pytest.fixture()
def loader(snapshot):
    return lambda repo, cfg: snapshot


def actor_factory(notifier=None):
    return lambda repo, snap, run_id, cfg: Actor(repo, snap, run_id, cfg, notifier=notifier or FakeNotifier())


def _run(repo, cfg, loader, **kw):
    statuses: list[tuple[str, str]] = []
    kw.setdefault("actor_factory", actor_factory())
    rep = run_once(repo, cfg, load=loader, status=lambda p, d: statuses.append((p, d)), **kw)
    return rep, statuses


def _no_ids(obj, allow: list[str] = ()) -> None:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    for a in allow:                                  # task ids are the one id the log may carry (SPEC §5)
        text = text.replace(a, "")
    for pat in ID_PATTERNS:
        assert not re.search(pat, text), (pat, text[:300])


# --------------------------------------------------------------------------
def test_every_candidate_is_logged_as_it_is_decided(repo, cfg, loader, snapshot, frozen_clock):
    expected = scan(snapshot, cfg)
    rep, statuses = _run(repo, cfg, loader, investigator_factory=factory(fail_on={"SILENT_ROOM"}))

    assert len(expected) == 6 and len(rep.results) == 6 and len(rep.log_ids) == 6 == len(repo.logs)
    assert rep.status == RUN_PARTIAL and rep.decided == 5 and rep.failed == 1
    assert rep.decisions == {"ACT": 4, "WATCH": 1}          # 2 spikes + 2 deadlines ACT, valve WATCH, silent room failed
    inv = FakeInvestigator.created[0]
    assert inv.run_id == rep.run_id and inv.seen == [c.key for c in expected] and inv.closed
    assert rep.tasks_created == 2 and rep.notifications_sent == 0        # 2 spike tasks; demo has no device tokens
    assert len(rep.actions) == 6 and sum(1 for a in rep.actions if a) == 4

    by_signal: dict[str, list[dict]] = {}
    for e in repo.logs:
        _no_ids(e, allow=[t.id for t in repo.get_sentinel_tasks()] + [t.id for t in repo.tasks.values()])
        assert e["runId"] == rep.run_id and e["farmName"] == snapshot.structure.farm.name
        assert e["timestamp"] == frozen_clock.now()
        by_signal.setdefault(e["signalType"], []).append(e)
    failed = by_signal["SILENT_ROOM"][0]
    assert failed["status"] == STATUS_FAILED and failed["decision"] is None and failed["error"].startswith("ValueError")
    assert failed["actionTaken"] is None
    for spike in by_signal["MORTALITY_SPIKE"]:
        assert spike["status"] == STATUS_DECIDED and spike["decision"] == "ACT" and spike["proposedAction"]["kind"] == "task"
        act = spike["actionTaken"]
        assert act["type"] == "task" and act["status"] == "done" and act["taskId"] in repo.tasks
        assert act["assigneeName"] == "Kovács Péter" and act["notifications"][0]["status"] == "skipped"
        assert spike["roomName"].endswith(repo.tasks[act["taskId"]].terem_nev)     # "H1 / 3. terem" vs "3. terem"
    for dl in by_signal["DEADLINE_RISK"]:
        assert dl["proposedAction"]["kind"] == "notification"
        act = dl["actionTaken"]
        assert act["type"] == "notification" and act["status"] == "skipped" and act["taskId"] in repo.tasks
        assert repo.tasks[act["taskId"]].sentinel_notify_count == 1
    assert by_signal["VALVE_INSTABILITY"][0]["watchReason"] == "More flapping tomorrow."
    assert by_signal["VALVE_INSTABILITY"][0]["actionTaken"] is None

    # the log is written per candidate, in between investigations, not in a batch at the end
    order = [s[0] for s in statuses]
    first_logged = order.index("logged")
    assert "investigating" in order[first_logged + 1:], "later candidates were investigated after the first was logged"


def test_run_doc_lifecycle(repo, cfg, loader, frozen_clock):
    rep, _ = _run(repo, cfg, loader, investigator_factory=factory(fail_on={"SILENT_ROOM"}), trigger="scheduler")
    doc = repo.runs[rep.run_id]
    assert doc["status"] == RUN_PARTIAL and doc["phase"] == PHASE_DONE and doc["currentTarget"] is None
    assert doc["startedAt"] == frozen_clock.now() and doc["finishedAt"] == frozen_clock.now()
    assert doc["trigger"] == "scheduler" and doc["model"] == cfg.gemini_model
    assert doc["candidates"] == 6 and doc["handled"] == 0 and doc["decided"] == 5 and doc["failed"] == 1
    assert doc["decisions"] == {"NOISE": 0, "WATCH": 1, "ACT": 4}
    assert doc["tasksCreated"] == 2 and doc["notificationsSent"] == 0
    assert doc["tokens"] == 6 * 60 and doc["lastLogId"] == rep.log_ids[-1] and doc["error"] is None
    assert doc["farmName"].startswith("Kisréti")

    upserts = [c[2] for c in repo.calls if c[0] == "upsert_run" and c[1] == rep.run_id]
    phases = [u["phase"] for u in upserts if "phase" in u]
    assert phases[0] == PHASE_SCANNING and phases[-1] == PHASE_DONE and PHASE_INVESTIGATING in phases and PHASE_ACTING in phases
    targets = [u["currentTarget"] for u in upserts if u.get("currentTarget")]
    assert len(targets) == 6 and all(" — " in t for t in targets)   # "SIGNAL — room name", names only
    _no_ids(targets)
    assert upserts[0]["status"] == RUN_RUNNING


def test_quiet_farm_is_an_ok_run_with_nothing_logged(repo, cfg, snapshot):
    now = snapshot.now
    quiet = replace(snapshot, mortality_today={}, valve_changes_today={},
                    open_tasks=[replace(t, deadline=now + timedelta(days=7)) for t in snapshot.open_tasks],
                    valves={rid: [replace(v, last_modified=now - timedelta(hours=2)) for v in vs] for rid, vs in snapshot.valves.items()})
    rep, statuses = _run(repo, cfg, lambda r, c: quiet, investigator_factory=factory())
    assert rep.status == RUN_OK and rep.candidates == [] and rep.log_ids == [] and not getattr(repo, "logs", [])
    assert FakeInvestigator.created == []                        # no LLM was even built
    doc = repo.runs[rep.run_id]
    assert doc["status"] == RUN_OK and doc["phase"] == PHASE_DONE and doc["candidates"] == 0
    assert ("done", "ok: 0 decided, 0 failed") in statuses


def test_overlapping_run_is_refused(repo, cfg, loader, frozen_clock):
    now = frozen_clock.now()
    repo.upsert_run("older", status=RUN_RUNNING, startedAt=now - timedelta(minutes=5), phase=PHASE_INVESTIGATING)
    rep, statuses = _run(repo, cfg, loader, investigator_factory=factory())
    assert rep.status == RUN_SKIPPED and "5 min ago" in rep.skip_reason and rep.log_ids == []
    assert FakeInvestigator.created == []
    doc = repo.runs[rep.run_id]
    assert doc["status"] == RUN_SKIPPED and doc["skipReason"] == rep.skip_reason and doc["finishedAt"] == now
    assert repo.runs["older"]["status"] == RUN_RUNNING            # the other run is left alone
    assert statuses == [("skipped", rep.skip_reason)]


def test_stale_running_marker_does_not_block(repo, cfg, loader, frozen_clock):
    """A crashed process can leave status=running behind; after 2 scan intervals it is ignored."""
    now = frozen_clock.now()
    repo.upsert_run("crashed", status=RUN_RUNNING, startedAt=now - timedelta(hours=3))
    repo.upsert_run("finished", status=RUN_OK, startedAt=now - timedelta(minutes=3), finishedAt=now - timedelta(minutes=2))
    rep, _ = _run(repo, cfg, loader, investigator_factory=factory())
    assert rep.status == RUN_OK and len(rep.log_ids) == 6


def test_scan_failure_is_logged_loudly(repo, cfg):
    def broken(repo, cfg):
        raise RuntimeError("Firestore unavailable")

    rep, statuses = _run(repo, cfg, broken, investigator_factory=factory())
    assert rep.status == RUN_FAILED and rep.error == "RuntimeError: Firestore unavailable"
    assert len(repo.logs) == 1
    e = repo.logs[0]
    assert e["signalType"] == SIGNAL_RUN and e["status"] == STATUS_RUN_FAILED and e["phase"] == PHASE_SCANNING
    assert e["error"] == rep.error and e["runId"] == rep.run_id
    doc = repo.runs[rep.run_id]
    assert doc["status"] == RUN_FAILED and doc["phase"] == PHASE_DONE and doc["error"] == rep.error and doc["finishedAt"] is not None
    assert statuses[-1] == ("failed", rep.error)


def test_investigator_crash_keeps_what_was_decided(repo, cfg, loader):
    """A hard exception mid-run: the entries logged so far stay, the crash is logged, the run is failed."""
    rep, _ = _run(repo, cfg, loader, investigator_factory=factory(crash_on="SILENT_ROOM"))
    assert rep.status == RUN_FAILED and rep.error == "RuntimeError: Vertex exploded"
    seen = FakeInvestigator.created[0].seen
    crashed_at = next(i for i, k in enumerate(seen) if k.startswith("SILENT_ROOM"))
    assert len(rep.results) == crashed_at and len(repo.logs) == crashed_at + 1     # decided ones + the failure entry
    assert repo.logs[-1]["status"] == STATUS_RUN_FAILED and repo.logs[-1]["phase"] == PHASE_INVESTIGATING
    assert FakeInvestigator.created[0].closed                                     # the runner is closed even on crash
    doc = repo.runs[rep.run_id]
    assert doc["status"] == RUN_FAILED and doc["decided"] == crashed_at


def test_all_investigations_failing_is_a_failed_run(repo, cfg, loader):
    rep, _ = _run(repo, cfg, loader, investigator_factory=factory(fail_on={"MORTALITY_SPIKE", "DEADLINE_RISK", "SILENT_ROOM", "VALVE_INSTABILITY"}))
    assert rep.status == RUN_FAILED and rep.error == "every investigation failed"
    assert len(repo.logs) == 6 and all(e["status"] == STATUS_FAILED for e in repo.logs)


def test_run_id_and_report_are_plain(repo, cfg, loader):
    rep, _ = _run(repo, cfg, loader, investigator_factory=factory())
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", rep.run_id)
    d = rep.as_dict()
    json.dumps(d)                                   # serialisable as-is (the CLI --json path), datetimes included
    assert d["candidates"] == 6 and d["decided"] == 6 and d["status"] == RUN_OK and d["logIds"] == rep.log_ids
    assert d["tasksCreated"] == 2 and len(d["actions"]) == 6 and isinstance(d["actions"][0]["sentAt"], str)
    _no_ids(d, allow=[t.id for t in repo.tasks.values()])


def test_sentinel_run_is_reusable_async(repo, cfg, loader):
    run = SentinelRun(repo, cfg, load=loader, investigator_factory=factory(), actor_factory=actor_factory())
    rep = asyncio.run(run.execute_async())
    assert rep is run.report and rep.status == RUN_OK


def test_second_run_skips_what_the_first_one_handled(repo, cfg, snapshot):
    """Idempotent runs (SPEC §8): 15 minutes later the same farm state must not double up."""
    live_loader = lambda r, c: replace(snapshot, open_tasks=r.get_open_tasks())      # what load_snapshot would see
    first, _ = _run(repo, cfg, live_loader, investigator_factory=factory())
    assert first.status == RUN_OK and first.tasks_created == 2 and len(first.handled) == 0
    tasks_after_first = len(repo.get_sentinel_tasks())

    second, statuses = _run(repo, cfg, live_loader, investigator_factory=factory())
    assert second.status == RUN_OK
    handled = {(c.signal_type, c.room_name) for c, _ in second.handled}
    # 2 spikes now have open Sentinel tasks, 2 deadlines were notified about → 4 handled
    assert len(handled) == 4 and {s for s, _ in handled} == {"MORTALITY_SPIKE", "DEADLINE_RISK"}
    # what is left is what still needs judgement: the valve (WATCH) and the silent room (NOISE with this fake)
    assert {(c.signal_type, c.room_name) for c in second.candidates} == {
        ("VALVE_INSTABILITY", "H1 / 4. terem"), ("SILENT_ROOM", "H2 / 6. terem")}
    assert second.tasks_created == 0 and len(repo.get_sentinel_tasks()) == tasks_after_first   # nothing was doubled
    assert FakeInvestigator.created[1].seen == [c.key for c in second.candidates]   # handled ones never reached the LLM
    doc = repo.runs[second.run_id]
    assert doc["handled"] == 4 and doc["candidates"] == 2
    assert any(p == "scanning" and "4 already handled" in d for p, d in statuses)
