"""Phase 5 — follow-up on the Sentinel's own tasks: escalation past the window
(severity up, farm manager notified, stamped, logged with `escalation`),
one-off outcome entries for closed tasks, nothing inside the window, errors
recorded not raised. Deterministic; FCM is faked; no Firestore."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import timedelta

import pytest

from sentinel.actions import DONE, FAILED, SENT, SKIPPED
from sentinel.config import Settings
from sentinel.followup import KIND_ESCALATION, KIND_OUTCOME, FollowUp, next_severity
from sentinel.logbook import STATUS_CLOSED, STATUS_ESCALATED
from sentinel.run import PHASE_FOLLOWING_UP, RUN_OK, RUN_PARTIAL, run_once
from tests.fakes import FakeNotifier, FakeRepository, snapshot_from_plan
from tests.test_run import actor_factory, factory

ID_PATTERNS = [r"\b[ab]-telep-t\d", r"-v\d+\b", r"demo-farm", r"demo-(admin|worker)", r"pigops-demo\.example"]
ADMIN_UID = "demo-admin"


@pytest.fixture()
def cfg():
    return Settings(_env_file=None)


@pytest.fixture()
def repo(demo_plan, frozen_clock):
    return FakeRepository(demo_plan, frozen_clock)


@pytest.fixture()
def snapshot(demo_plan, frozen_clock):
    return snapshot_from_plan(demo_plan, frozen_clock)


@pytest.fixture()
def notifier():
    return FakeNotifier()


@pytest.fixture()
def fu(repo, snapshot, cfg, notifier):
    return FollowUp(repo, snapshot, "run-2", cfg, notifier=notifier)


def _room(snapshot, label):
    return next(r for r in snapshot.structure.rooms if r.display_name == label)


def _sentinel_task(repo, snapshot, label, *, signal="SILENT_ROOM", severity="medium", hours_ago=30.0, title=None):
    """A task the Sentinel created `hours_ago` hours ago (created via the same repository call the actor uses)."""
    room = _room(snapshot, label)
    t = repo.create_task(room=room, title=title or f"Check {label}", description="Please look.", assignee_email=repo.users[ADMIN_UID].email,
                         severity=severity, signal_type=signal, run_id="run-1", deadline=repo.clock.now(), signal_key=f"{signal}:{room.id}")
    t.created_at = repo.clock.now() - timedelta(hours=hours_ago)
    t.work_log_count = 0
    return t


def _no_ids(obj, allow=()) -> None:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    for a in allow:
        text = text.replace(a, "")
    for pat in ID_PATTERNS:
        assert not re.search(pat, text), (pat, text[:300])


# ------------------------------------------------------------------ helpers
def test_next_severity():
    assert next_severity("low") == "medium" and next_severity("medium") == "high" and next_severity("high") == "high"
    assert next_severity(None) == "medium"


# --------------------------------------------------------------- escalation
def test_open_task_past_the_window_is_escalated(fu, repo, snapshot, frozen_clock, notifier):
    t = _sentinel_task(repo, snapshot, "B-Telep / 6.Terem", severity="medium", hours_ago=30)
    rep = fu.run()

    assert rep.checked == 1 and rep.escalated == 1 and rep.closed == 0 and rep.error is None
    item = rep.items[0]
    assert item.kind == KIND_ESCALATION and item.task.id == t.id and item.room_name == "B-Telep / 6.Terem"
    e = item.entry
    assert e["status"] == STATUS_ESCALATED and e["phase"] == "follow-up" and e["runId"] == "run-2"
    assert e["decision"] == "ACT" and e["severity"] == "high" and e["signalType"] == "SILENT_ROOM"
    assert e["escalation"] == {"fromSeverity": "medium", "toSeverity": "high", "reason": e["reasoning"]}
    assert "30 h" in e["reasoning"] and "24 h escalation window" in e["reasoning"] and "medium → high" in e["reasoning"]
    assert "no work-log entries" in e["observation"] and "Fenyvesi Márton" in e["observation"] and t.title in e["observation"]
    act = e["actionTaken"]
    assert act["tool"] == "escalate_task" and act["type"] == "escalation" and act["taskId"] == t.id
    assert act["status"] == "skipped" and act["notifications"] == [{"to": "Fenyvesi Márton (farm manager)", "status": "skipped",
                                                                      "reason": "no device token registered for Fenyvesi Márton"}]
    # the task itself: severity raised and stamped
    assert t.severity == "high" and t.escalated_at == frozen_clock.now() and t.escalation_count == 1
    assert notifier.sent == []
    _no_ids(e, allow=[t.id])
    assert "escalated medium → high" in item.summary


def test_low_goes_to_medium_high_stays_high_but_still_nudges(fu, repo, snapshot):
    low = _sentinel_task(repo, snapshot, "A-Telep / 4.Terem", signal="VALVE_INSTABILITY", severity="low", hours_ago=26)
    high = _sentinel_task(repo, snapshot, "B-Telep / 5.Terem", signal="MORTALITY_SPIKE", severity="high", hours_ago=26)
    rep = fu.run()
    by = {i.task.id: i for i in rep.items}
    assert by[low.id].entry["escalation"]["toSeverity"] == "medium" and low.severity == "medium"
    assert by[high.id].entry["escalation"] == {"fromSeverity": "high", "toSeverity": "high", "reason": by[high.id].entry["reasoning"]}
    assert "highest severity" in by[high.id].entry["reasoning"] and high.escalation_count == 1
    assert len(by[high.id].action.notifications) == 1                 # the manager is nudged anyway


def test_nothing_inside_the_window_and_repeat_after_another_window(fu, repo, snapshot, frozen_clock):
    fresh = _sentinel_task(repo, snapshot, "B-Telep / 6.Terem", hours_ago=5)
    recently = _sentinel_task(repo, snapshot, "A-Telep / 4.Terem", signal="VALVE_INSTABILITY", hours_ago=50)
    recently.escalated_at, recently.escalation_count = frozen_clock.now() - timedelta(hours=2), 1
    assert fu.run().items == []                                        # 5 h old; escalated 2 h ago → nothing

    recently.escalated_at = frozen_clock.now() - timedelta(hours=25)   # a full window since the last escalation
    rep = fu.run()
    assert [i.task.id for i in rep.items] == [recently.id]
    assert "already escalated 1×" in rep.items[0].entry["reasoning"] and recently.escalation_count == 2
    assert fresh.escalation_count == 0


def test_mortality_context_is_in_the_reason(fu, repo, snapshot, frozen_clock):
    t = _sentinel_task(repo, snapshot, "B-Telep / 5.Terem", signal="MORTALITY_SPIKE", severity="high", hours_ago=30)
    room = _room(snapshot, "B-Telep / 5.Terem")
    expected = sum(e.darab for e in repo.get_mortality_events(t.created_at, frozen_clock.now(), room.id))
    assert expected >= 5                                               # today's planted spike at least
    rep = fu.run()
    assert f"{expected} more deaths in B-Telep / 5.Terem since it was created" in rep.items[0].entry["reasoning"]


def test_escalation_notification_is_sent_when_the_manager_has_a_device(repo, snapshot, cfg):
    repo.users[ADMIN_UID].fcm_token = "adm"
    notifier = FakeNotifier()
    t = _sentinel_task(repo, snapshot, "B-Telep / 6.Terem", hours_ago=30)
    rep = FollowUp(repo, snapshot, "run-2", cfg, notifier=notifier).run()
    act = rep.items[0].action
    assert act.status == DONE and act.notifications[0].status == SENT and act.notifications_sent == 1
    uid, title, body, data = notifier.sent[0]
    assert uid == ADMIN_UID and title.startswith("Escalated (high): ") and "escalation window" in body
    assert data["kind"] == "escalation" and data["taskId"] == t.id and data["roomName"] == "B-Telep / 6.Terem"


# ------------------------------------------------------------------ outcome
def test_closed_task_outcome_is_logged_exactly_once(fu, repo, snapshot, frozen_clock):
    t = _sentinel_task(repo, snapshot, "B-Telep / 5.Terem", signal="MORTALITY_SPIKE", severity="high", hours_ago=30)
    repo.close_task(t.id, frozen_clock.now() - timedelta(hours=4))
    rep = fu.run()

    assert rep.checked == 1 and rep.closed == 1 and rep.escalated == 0
    item = rep.items[0]
    assert item.kind == KIND_OUTCOME and item.action is None
    e = item.entry
    assert e["status"] == STATUS_CLOSED and e["decision"] is None and e["severity"] == "high" and e["escalation"] is None
    assert e["actionTaken"] is None and e["taskId"] == t.id and e["signalType"] == "MORTALITY_SPIKE"
    o = e["outcome"]
    assert o["openHours"] == 26.0 and o["closedAt"] == frozen_clock.now() - timedelta(hours=4)
    assert o["escalations"] == 0 and o["createdByRun"] == "run-1" and o["deathsSince"] >= 0 and o["workLogEntries"] == 0
    assert "closed 26 h after it was created" in e["observation"] and "Fenyvesi Márton" in e["observation"]
    assert t.sentinel_outcome_logged_at == frozen_clock.now()
    _no_ids(e, allow=[t.id])

    assert fu.run().items == []                                        # never twice


def test_closed_task_never_escalates(fu, repo, snapshot, frozen_clock):
    t = _sentinel_task(repo, snapshot, "B-Telep / 6.Terem", hours_ago=60)
    t.escalation_count, t.escalated_at = 1, frozen_clock.now() - timedelta(hours=36)
    repo.close_task(t.id, frozen_clock.now() - timedelta(hours=1))
    rep = fu.run()
    assert [i.kind for i in rep.items] == [KIND_OUTCOME]
    assert "escalated 1× before it was closed" in rep.items[0].entry["observation"]
    assert t.escalation_count == 1                                     # untouched by the follow-up


# ------------------------------------------------------------------- errors
def test_an_error_on_one_task_does_not_stop_the_others(fu, repo, snapshot):
    bad = _sentinel_task(repo, snapshot, "B-Telep / 6.Terem", hours_ago=30)
    good = _sentinel_task(repo, snapshot, "A-Telep / 4.Terem", signal="VALVE_INSTABILITY", hours_ago=31)
    original = repo.escalate_task

    def boom(task_id, severity, at):
        if task_id == bad.id:
            raise RuntimeError("Firestore write refused")
        return original(task_id, severity, at)

    repo.escalate_task = boom
    rep = fu.run()
    by = {i.task.id: i for i in rep.items}
    assert by[bad.id].entry["status"] == "failed" and "RuntimeError" in by[bad.id].entry["error"]
    assert by[bad.id].action.status == FAILED
    assert by[good.id].entry["status"] == STATUS_ESCALATED and good.escalation_count == 1


def test_listing_failure_is_reported_not_raised(repo, snapshot, cfg, notifier):
    def boom(include_done=True):
        raise RuntimeError("Firestore unavailable")

    repo.get_sentinel_tasks = boom
    rep = FollowUp(repo, snapshot, "run-2", cfg, notifier=notifier).run()
    assert rep.items == [] and rep.error == "RuntimeError: Firestore unavailable"


# -------------------------------------------------------------- in the run
def test_run_follows_up_after_deciding(repo, snapshot, cfg, frozen_clock):
    old = _sentinel_task(repo, snapshot, "B-Telep / 6.Terem", hours_ago=30)                      # → escalation
    done = _sentinel_task(repo, snapshot, "A-Telep / 4.Terem", signal="VALVE_INSTABILITY", hours_ago=40)
    repo.close_task(done.id, frozen_clock.now() - timedelta(hours=10))                            # → outcome
    live_loader = lambda r, c: replace(snapshot, open_tasks=r.get_open_tasks())
    statuses: list[tuple[str, str]] = []
    rep = run_once(repo, cfg, load=live_loader, status=lambda p, d: statuses.append((p, d)),
                   investigator_factory=factory(), actor_factory=actor_factory(),
                   followup_factory=lambda r, s, run_id, c: FollowUp(r, s, run_id, c, notifier=FakeNotifier()))

    assert rep.status == RUN_OK and rep.followup is not None
    fu = rep.followup
    # the two old tasks + the ones this very run created (all checked, only the old ones qualify)
    assert fu.checked == 2 + rep.tasks_created and fu.escalated == 1 and fu.closed == 1
    assert all(i.log_id in rep.log_ids for i in fu.items)
    kinds = {e["status"] for e in repo.logs}
    assert {STATUS_ESCALATED, STATUS_CLOSED} <= kinds
    doc = repo.runs[rep.run_id]
    assert doc["followedUp"] == fu.checked and doc["escalated"] == 1 and doc["closedOutcomes"] == 1
    assert doc["lastLogId"] == rep.log_ids[-1]
    phases = [c[2]["phase"] for c in repo.calls if c[0] == "upsert_run" and c[1] == rep.run_id and "phase" in c[2]]
    assert PHASE_FOLLOWING_UP in phases and phases.index(PHASE_FOLLOWING_UP) > phases.index("investigating")
    assert any(p == "logged" and "escalated medium → high" in d for p, d in statuses)
    assert any(p == "done" and "1 escalated, 1 closed" in d for p, d in statuses)
    d = rep.as_dict()
    json.dumps(d)
    assert d["followUp"]["escalated"] == 1 and d["followUp"]["closed"] == 1 and len(d["followUp"]["items"]) == 2
    # the escalation must not have been re-flagged by the scanner as a DEADLINE_RISK either
    assert not any(c.signal_type == "DEADLINE_RISK" and c.task and c.task.is_sentinel_task for c in rep.candidates)


def test_quiet_run_still_follows_up(repo, snapshot, cfg, frozen_clock):
    """Most runs find no candidates — the follow-up must run regardless."""
    old = _sentinel_task(repo, snapshot, "B-Telep / 6.Terem", hours_ago=30)
    now = snapshot.now
    quiet = replace(snapshot, mortality_today={}, valve_changes_today={},
                    open_tasks=[replace(t, deadline=now + timedelta(days=7)) for t in snapshot.open_tasks],
                    valves={rid: [replace(v, last_modified=now - timedelta(hours=2)) for v in vs] for rid, vs in snapshot.valves.items()})
    rep = run_once(repo, cfg, load=lambda r, c: quiet, investigator_factory=factory(), actor_factory=actor_factory(),
                   followup_factory=lambda r, s, run_id, c: FollowUp(r, s, run_id, c, notifier=FakeNotifier()))
    assert rep.status == RUN_OK and rep.candidates == []
    assert rep.followup.escalated == 1 and len(rep.log_ids) == 1 and old.severity == "high"


def test_follow_up_error_makes_the_run_partial(repo, snapshot, cfg):
    def boom(include_done=True):
        raise RuntimeError("Firestore unavailable")

    repo.get_sentinel_tasks = boom
    live_loader = lambda r, c: replace(snapshot, open_tasks=r.get_open_tasks())
    rep = run_once(repo, cfg, load=live_loader, investigator_factory=factory(), actor_factory=actor_factory(),
                   followup_factory=lambda r, s, run_id, c: FollowUp(r, s, run_id, c, notifier=FakeNotifier()))
    assert rep.status == RUN_PARTIAL and rep.error.startswith("follow-up: RuntimeError")
    assert repo.runs[rep.run_id]["status"] == RUN_PARTIAL
