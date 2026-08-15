"""Phase 4 — the action tools and their guardrails: task creation with assignee
resolution, dedup window, per-run cap, deadline notifications (assignee +
farm manager, once), notification dedup on the task, failures recorded not
raised. FCM is faked; no Firestore."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import timedelta

import pytest

from sentinel import schema as S
from sentinel.actions import CAPPED, DEDUPLICATED, DONE, FAILED, SENT, SKIPPED, Actor, FcmNotifier
from sentinel.agent.investigator import InvestigationResult
from sentinel.agent.schemas import Decision
from sentinel.config import Settings
from sentinel.scanner import partition_handled, scan
from sentinel.schema import User
from tests.fakes import FakeNotifier, FakeRepository, snapshot_from_plan

ID_PATTERNS = [r"\b[ab]-telep-t\d", r"-v\d+\b", r"demo-farm", r"\btask-[a-z]", r"demo-(admin|worker)", r"pigops-demo\.example"]
ADMIN_UID, WORKER1_UID, WORKER2_UID = "demo-admin", "demo-worker-1", "demo-worker-2"


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
def cands(snapshot, cfg):
    """One candidate per (signal, room) from the demo plan."""
    return {(c.signal_type, c.room_name): c for c in scan(snapshot, cfg)}


@pytest.fixture()
def notifier():
    return FakeNotifier()


@pytest.fixture()
def actor(repo, snapshot, cfg, notifier):
    return Actor(repo, snapshot, "run-1", cfg, notifier=notifier)


def _act(candidate, severity="high", title="Do the thing", body="Details here.") -> InvestigationResult:
    d = Decision(decision="ACT", severity=severity, reasoning="Because.", action_title=title, action_body=body)
    return InvestigationResult(candidate=candidate, decision=d, raw_output="{}", tool_trace=[], latency_ms=1)


def _result(candidate, decision) -> InvestigationResult:
    return InvestigationResult(candidate=candidate, decision=decision, raw_output="{}", tool_trace=[], latency_ms=1)


def _no_ids(obj, allow_task_id: str | None = None) -> None:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    if allow_task_id:
        text = text.replace(allow_task_id, "")
    for pat in ID_PATTERNS:
        assert not re.search(pat, text), (pat, text[:300])


def _give_token(repo, snapshot, uid: str, token: str = "tok") -> None:
    """Both places a user can be looked up from: the repository (manager) and the snapshot (assignee)."""
    repo.users[uid].fcm_token = token
    for u in snapshot.users_by_email.values():
        if u.uid == uid:
            u.fcm_token = token


# ------------------------------------------------------------------ create_task
def test_spike_creates_a_task_for_the_farm_admin_and_notifies(actor, repo, cands, frozen_clock, notifier):
    c = cands[("MORTALITY_SPIKE", "B-Telep / 5.Terem")]
    out = actor.act(_act(c, "high", "Urgent respiratory check", "Call the vet."))

    assert out.type == "task" and out.status == DONE and out.tool == "create_task"
    task = repo.tasks[out.task_id]
    assert task.is_sentinel_task and task.signal_type == "MORTALITY_SPIKE" and task.terem_id == c.room.id
    assert task.title == "Urgent respiratory check" and task.description == "Call the vet." and task.severity == "high"
    assert task.assignee_email == repo.users[ADMIN_UID].email          # farm-level admin = Fenyvesi Márton
    assert task.sentinel_run_id == "run-1" and not task.done
    assert out.assignee_name == "Fenyvesi Márton" and out.assignee_note is None
    # high severity → within 4 hours (frozen 10:30 local → 14:30), never past the end of the day
    assert out.deadline == frozen_clock.now() + timedelta(hours=4) == task.deadline
    assert out.notifications and out.notifications[0].to == "Fenyvesi Márton" and out.notifications[0].status == SKIPPED
    assert "no device token" in out.notifications[0].reason and notifier.sent == []
    assert actor.tasks_created == 1 and actor.notifications_sent == 0 and actor.notifications_skipped == 1

    log = out.as_log()
    assert log["type"] == "task" and log["status"] == "done" and log["taskId"] == task.id and log["taskTitle"] == task.title
    assert log["assigneeName"] == "Fenyvesi Márton" and "assigneeNote" not in log and log["notifications"][0]["status"] == "skipped"
    _no_ids(log, allow_task_id=task.id)
    assert isinstance(out.as_log(json_safe=True)["deadline"], str)


def test_medium_and_low_severity_get_the_end_of_the_day(actor, repo, cands, frozen_clock):
    c = cands[("VALVE_INSTABILITY", "A-Telep / 4.Terem")]
    out = actor.act(_act(c, "low"))
    assert out.deadline == repo.clock.day_bounds(repo.clock.today())[1]


def test_task_notification_is_sent_when_the_admin_has_a_device(actor, repo, snapshot, cands, notifier):
    _give_token(repo, snapshot, ADMIN_UID)
    c = cands[("SILENT_ROOM", "B-Telep / 6.Terem")]
    out = actor.act(_act(c, "high", "Walk through B-Telep / 6.Terem", "Nobody has looked in for 3 days."))
    assert out.status == DONE and out.notifications[0].status == SENT and actor.notifications_sent == 1
    uid, title, body, data = notifier.sent[0]
    assert uid == ADMIN_UID and title == "Walk through B-Telep / 6.Terem" and body.startswith("Nobody")
    assert data["source"] == "sentinel" and data["signalType"] == "SILENT_ROOM" and data["roomName"] == "B-Telep / 6.Terem"
    assert data["taskId"] == out.task_id and data["runId"] == "run-1"


def test_second_task_for_the_same_room_and_signal_is_deduplicated(actor, repo, cands):
    c = cands[("MORTALITY_SPIKE", "B-Telep / 5.Terem")]
    first = actor.act(_act(c, "high", "First"))
    second = actor.act(_act(c, "high", "Second"))
    assert first.status == DONE and second.status == DEDUPLICATED
    assert second.task_id == first.task_id and second.task_title == "First"
    assert "B-Telep / 5.Terem" in second.reason and "open Sentinel task" in second.reason and "'First'" in second.reason
    assert actor.tasks_created == 1 and len(repo.get_sentinel_tasks()) == 1
    assert second.notifications == []                                    # nothing was sent for the duplicate


def test_recently_closed_sentinel_task_still_blocks_a_new_one(actor, repo, cands, frozen_clock):
    c = cands[("MORTALITY_SPIKE", "B-Telep / 5.Terem")]
    first = actor.act(_act(c))
    repo.tasks[first.task_id].done = True
    repo.tasks[first.task_id].created_at = frozen_clock.now() - timedelta(hours=5)     # inside the 48 h window
    again = actor.act(_act(c))
    assert again.status == DEDUPLICATED and "recently closed" in again.reason and "5 h ago" in again.reason


def test_per_run_task_cap(repo, snapshot, cands, notifier):
    actor = Actor(repo, snapshot, "run-1", Settings(_env_file=None, max_tasks_per_run=1), notifier=notifier)
    a = actor.act(_act(cands[("MORTALITY_SPIKE", "B-Telep / 5.Terem")]))
    b = actor.act(_act(cands[("SILENT_ROOM", "B-Telep / 6.Terem")]))
    assert a.status == DONE and b.status == CAPPED and "cap of 1" in b.reason and b.task_id is None
    assert actor.tasks_created == 1 and len(repo.get_sentinel_tasks()) == 1


def test_no_responsible_person_creates_an_unassigned_task_and_says_so(repo, snapshot, cands, cfg, notifier):
    repo.perms = {uid: (S.ROLE_USER, ts) for uid, (_, ts) in repo.perms.items()}    # nobody is admin on the farm
    actor = Actor(repo, snapshot, "run-1", cfg, notifier=notifier)
    out = actor.act(_act(cands[("MORTALITY_SPIKE", "B-Telep / 5.Terem")]))
    assert out.status == DONE and out.assignee_name is None
    assert out.assignee_note and "no responsible person" in out.assignee_note
    assert repo.tasks[out.task_id].assignee_email is None and out.notifications == []


# ------------------------------------------------------------ send_notification
def test_deadline_risk_notifies_assignee_and_manager_and_stamps_the_task(actor, repo, cands, frozen_clock, notifier):
    c = cands[("DEADLINE_RISK", "A-Telep / 2.Terem")]        # assigned to Halászi Réka (worker), overdue
    assert c.task is not None and c.task.assignee_email == repo.users[WORKER1_UID].email
    out = actor.act(_act(c, "high", "Overdue fan repair", "43 h overdue, please act."))

    assert out.type == "notification" and out.tool == "send_notification" and out.task_id == c.task.id
    assert out.task_title == c.task.title and out.assignee_name == "Halászi Réka"
    assert [n.to for n in out.notifications] == ["Halászi Réka (assignee)", "Fenyvesi Márton (farm manager)"]
    assert all(n.status == SKIPPED for n in out.notifications)          # demo: no device tokens
    assert out.status == SKIPPED and "no device tokens" in out.reason
    assert notifier.sent == []
    # stamped anyway: retrying without tokens would only nag the log every 15 minutes
    t = repo.tasks[c.task.id]
    assert t.sentinel_notified_at == frozen_clock.now() and t.sentinel_notify_count == 1
    assert len(repo.get_sentinel_tasks()) == 0                           # no task was created for a deadline
    _no_ids(out.as_log(), allow_task_id=c.task.id)


def test_deadline_notifications_are_sent_with_devices(actor, repo, snapshot, cands, notifier):
    _give_token(repo, snapshot, WORKER1_UID, "w1")
    _give_token(repo, snapshot, ADMIN_UID, "adm")
    c = cands[("DEADLINE_RISK", "A-Telep / 2.Terem")]
    out = actor.act(_act(c, "high", "Overdue fan repair", "Please act."))
    assert out.status == DONE and [n.status for n in out.notifications] == [SENT, SENT]
    assert {uid for uid, *_ in notifier.sent} == {WORKER1_UID, ADMIN_UID}
    assert notifier.sent[0][3]["taskId"] == c.task.id
    assert actor.notifications_sent == 2 and repo.tasks[c.task.id].sentinel_notify_count == 1


def test_deadline_notification_is_not_repeated_inside_the_window(actor, repo, cands, frozen_clock, notifier):
    c = cands[("DEADLINE_RISK", "A-Telep / 2.Terem")]
    c.task.sentinel_notified_at = frozen_clock.now() - timedelta(hours=2)
    repo.tasks[c.task.id].sentinel_notified_at = c.task.sentinel_notified_at
    out = actor.act(_act(c))
    assert out.status == DEDUPLICATED and "2 h ago" in out.reason and out.notifications == []
    assert repo.tasks[c.task.id].sentinel_notify_count == 0               # not stamped again


def test_all_notifications_failing_is_failed_and_not_stamped(repo, snapshot, cands, cfg):
    _give_token(repo, snapshot, WORKER1_UID, "w1")
    _give_token(repo, snapshot, ADMIN_UID, "adm")
    actor = Actor(repo, snapshot, "run-1", cfg, notifier=FakeNotifier(fail_for=(WORKER1_UID, ADMIN_UID)))
    c = cands[("DEADLINE_RISK", "A-Telep / 2.Terem")]
    out = actor.act(_act(c))
    assert out.status == FAILED and "every notification failed" in out.reason
    assert all(n.status == FAILED and "FirebaseError" in n.reason for n in out.notifications)
    assert repo.tasks[c.task.id].sentinel_notified_at is None             # will be retried next run


def test_partial_delivery_counts_as_done(repo, snapshot, cands, cfg):
    _give_token(repo, snapshot, WORKER1_UID, "w1")
    _give_token(repo, snapshot, ADMIN_UID, "adm")
    actor = Actor(repo, snapshot, "run-1", cfg, notifier=FakeNotifier(fail_for=(WORKER1_UID,)))
    out = actor.act(_act(cands[("DEADLINE_RISK", "A-Telep / 2.Terem")]))
    assert out.status == DONE and [n.status for n in out.notifications] == [FAILED, SENT]
    assert repo.tasks[out.task_id].sentinel_notify_count == 1


def test_assignee_who_is_the_manager_is_notified_once(actor, repo, snapshot, cands):
    c = cands[("DEADLINE_RISK", "A-Telep / 2.Terem")]
    admin_email = repo.users[ADMIN_UID].email
    c = replace(c, task=replace(c.task, assignee_email=admin_email))
    repo.tasks[c.task.id] = c.task
    out = actor.act(_act(c))
    assert [n.to for n in out.notifications] == ["Fenyvesi Márton (assignee)"]


def test_unknown_assignee_falls_back_to_the_manager(actor, repo, cands):
    c = cands[("DEADLINE_RISK", "A-Telep / 2.Terem")]
    c = replace(c, task=replace(c.task, assignee_email="somebody@elsewhere.example"))
    repo.tasks[c.task.id] = c.task
    out = actor.act(_act(c))
    assert [n.to for n in out.notifications] == ["Fenyvesi Márton (farm manager)"]
    assert out.assignee_name is None and "not among the farm's users" in out.assignee_note
    _no_ids(out.as_log(), allow_task_id=c.task.id)                       # the foreign email is not in the log either


# ------------------------------------------------------------------ dispatch
def test_non_act_decisions_do_nothing(actor, cands):
    c = cands[("MORTALITY_SPIKE", "A-Telep / 3.Terem")]
    assert actor.act(_result(c, Decision(decision="NOISE", reasoning="fine"))) is None
    assert actor.act(_result(c, Decision(decision="WATCH", reasoning="hm", watch_reason="more"))) is None
    assert actor.act(_result(c, None)) is None
    assert actor.tasks_created == 0


def test_an_exception_inside_an_action_becomes_a_failed_outcome(actor, repo, cands):
    def boom(**kw):
        raise RuntimeError("Firestore write refused")

    repo.create_task = boom
    out = actor.act(_act(cands[("MORTALITY_SPIKE", "B-Telep / 5.Terem")]))
    assert out.status == FAILED and out.tool == "create_task" and "RuntimeError: Firestore write refused" in out.reason


def test_fcm_notifier_skips_users_without_a_token_without_touching_firebase():
    status, reason = FcmNotifier().send(User(uid="u", email="a@b.c", display_name="Someone"), "t", "b", {})
    assert status == SKIPPED and "Someone" in reason


# ------------------------------------------------- scanner: already handled
def test_partition_handled_after_a_run(actor, repo, snapshot, cands, cfg):
    """After the actor did its work, the same scan does not re-flag what is already covered."""
    actor.act(_act(cands[("MORTALITY_SPIKE", "B-Telep / 5.Terem")]))          # new Sentinel task
    actor.act(_act(cands[("DEADLINE_RISK", "A-Telep / 2.Terem")]))            # notified (skipped, but stamped)
    snap2 = replace(snapshot, open_tasks=repo.get_open_tasks())            # what the next run would load
    fresh, handled = partition_handled(scan(snap2, cfg), snap2, cfg)
    handled_keys = {(c.signal_type, c.room_name): why for c, why in handled}
    assert set(handled_keys) == {("MORTALITY_SPIKE", "B-Telep / 5.Terem"), ("DEADLINE_RISK", "A-Telep / 2.Terem")}
    assert "open Sentinel task" in handled_keys[("MORTALITY_SPIKE", "B-Telep / 5.Terem")]
    assert "already notified" in handled_keys[("DEADLINE_RISK", "A-Telep / 2.Terem")]
    assert {(c.signal_type, c.room_name) for c in fresh} == {
        ("MORTALITY_SPIKE", "A-Telep / 3.Terem"), ("DEADLINE_RISK", "B-Telep / 3.Terem"),
        ("SILENT_ROOM", "B-Telep / 6.Terem"), ("VALVE_INSTABILITY", "A-Telep / 4.Terem"),
    }
    # the Sentinel's own new task must not come back as a DEADLINE_RISK candidate either
    assert not any(c.signal_type == "DEADLINE_RISK" and c.task and c.task.is_sentinel_task for c in fresh)


def test_partition_handled_expires_with_the_dedup_window(snapshot, cands, cfg, frozen_clock):
    c = cands[("DEADLINE_RISK", "A-Telep / 2.Terem")]
    old = replace(c.task, sentinel_notified_at=frozen_clock.now() - timedelta(hours=cfg.dedup_window_hours + 1))
    snap2 = replace(snapshot, open_tasks=[old if t.id == c.task.id else t for t in snapshot.open_tasks])
    fresh, handled = partition_handled(scan(snap2, cfg), snap2, cfg)
    assert handled == [] and len(fresh) == 6


def test_partition_handled_cools_down_identical_noise_and_watch(snapshot, cands, cfg, frozen_clock):
    """A NOISE/WATCH verdict on identical evidence is not re-litigated inside the cooldown; new evidence is."""
    chronic = cands[("MORTALITY_SPIKE", "A-Telep / 3.Terem")]
    valve = cands[("VALVE_INSTABILITY", "A-Telep / 4.Terem")]
    now = frozen_clock.now()
    recent = [
        {"status": "decided", "decision": "NOISE", "signalType": "MORTALITY_SPIKE", "roomName": chronic.room_name,
         "observation": chronic.observation, "timestamp": now - timedelta(minutes=20)},
        {"status": "decided", "decision": "WATCH", "signalType": "VALVE_INSTABILITY", "roomName": valve.room_name,
         "observation": valve.observation, "timestamp": now - timedelta(hours=cfg.decision_cooldown_hours + 1)},   # too old
        {"status": "decided", "decision": "ACT", "signalType": "SILENT_ROOM", "roomName": "B-Telep / 6.Terem",
         "observation": cands[("SILENT_ROOM", "B-Telep / 6.Terem")].observation, "timestamp": now - timedelta(minutes=5)},  # ACT never cools down
        {"status": "failed", "decision": None, "signalType": "DEADLINE_RISK", "roomName": "x", "observation": "y", "timestamp": now},
    ]
    fresh, handled = partition_handled(scan(snapshot, cfg), snapshot, cfg, recent)
    reasons = {(c.signal_type, c.room_name): why for c, why in handled}
    assert set(reasons) == {("MORTALITY_SPIKE", "A-Telep / 3.Terem")}
    assert reasons[("MORTALITY_SPIKE", "A-Telep / 3.Terem")] == "judged NOISE 20 min ago on identical evidence; nothing has changed"
    assert len(fresh) == 5

    # the evidence changes → the observation text changes → investigated again
    changed = [{**recent[0], "observation": chronic.observation.replace("4 deaths", "5 deaths")}]
    fresh, handled = partition_handled(scan(snapshot, cfg), snapshot, cfg, changed)
    assert handled == [] and len(fresh) == 6

    # cooldown switched off
    off = Settings(_env_file=None, decision_cooldown_hours=0)
    fresh, handled = partition_handled(scan(snapshot, off), snapshot, off, recent)
    assert handled == [] and len(fresh) == 6


def test_partition_handled_respects_a_recently_closed_task(actor, repo, snapshot, cands, cfg, frozen_clock):
    """The actor would refuse to re-open it (dedup window) — so the scanner does not send it to the LLM either."""
    out = actor.act(_act(cands[("VALVE_INSTABILITY", "A-Telep / 4.Terem")]))
    repo.close_task(out.task_id, frozen_clock.now() - timedelta(hours=1))
    snap2 = replace(snapshot, open_tasks=repo.get_open_tasks())
    fresh, handled = partition_handled(scan(snap2, cfg), snap2, cfg, [], repo.get_sentinel_tasks(include_done=True))
    reasons = {(c.signal_type, c.room_name): why for c, why in handled}
    assert set(reasons) == {("VALVE_INSTABILITY", "A-Telep / 4.Terem")}
    assert reasons[("VALVE_INSTABILITY", "A-Telep / 4.Terem")].startswith("a Sentinel task for it was closed 1 h ago")
    assert "48 h dedup window" in reasons[("VALVE_INSTABILITY", "A-Telep / 4.Terem")]
    # outside the window it comes back
    repo.tasks[out.task_id].created_at = frozen_clock.now() - timedelta(hours=cfg.dedup_window_hours + 1)
    fresh, handled = partition_handled(scan(snap2, cfg), snap2, cfg, [], repo.get_sentinel_tasks(include_done=True))
    assert handled == [] and len(fresh) == 6
