"""Phase 5 — Follow up: the Sentinel re-checks the tasks it created itself.

Runs on every tick, deterministically (no LLM: most runs are quiet and this
must cost nothing), over every task tagged ``source = "sentinel"``:

* **Still open past the escalation window** (``ESCALATION_HOURS`` since it was
  created, or since the last escalation) → *escalate*: raise the severity one
  step (low → medium → high; high stays high), notify the farm manager, stamp
  the task (``escalatedAt`` / ``escalationCount``) and write a log entry with
  ``escalation {fromSeverity, toSeverity, reason}``. The reason carries what
  changed in the room since the task was created — deaths since, work-log
  entries — so the manager sees *why it matters now*, not just *it is late*.
* **Closed** → write the outcome once (stamped ``sentinelOutcomeLoggedAt``):
  how long it stayed open, whether it was escalated, what happened in the room
  meanwhile. This is the seed of future learning (SPEC §4): the log starts to
  contain "what the agent asked for" next to "what came of it".

Everything here goes through the same log builder as the decisions and, like
the actions, an error becomes a ``failed`` outcome, never a crashed run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .actions import DONE, FAILED, SKIPPED, ActionOutcome, FcmNotifier, NotificationOutcome, ago
from .config import Settings, settings as default_settings
from .logbook import escalation_entry, outcome_entry
from .scanner import FarmSnapshot
from .schema import Task, User

log = logging.getLogger(__name__)

SEVERITY_ORDER = ["low", "medium", "high"]

KIND_ESCALATION = "escalation"
KIND_OUTCOME = "outcome"


def next_severity(current: str | None) -> str:
    if current not in SEVERITY_ORDER:
        return "medium"
    return SEVERITY_ORDER[min(SEVERITY_ORDER.index(current) + 1, len(SEVERITY_ORDER) - 1)]


@dataclass
class FollowUpItem:
    """One follow-up event (an escalation or a closed-task outcome) → one log entry."""

    kind: str                          # escalation | outcome
    task: Task
    room_name: str
    entry: dict[str, Any]              # the sentinelLog document (built, then written by the run)
    action: ActionOutcome | None = None
    log_id: str | None = None

    @property
    def summary(self) -> str:
        if self.kind == KIND_ESCALATION:
            e = self.entry["escalation"]
            return f"{self.room_name}: '{self.task.title}' escalated {e['fromSeverity']} → {e['toSeverity']}"
        return f"{self.room_name}: '{self.task.title}' closed — {self.entry['observation']}"


@dataclass
class FollowUpReport:
    items: list[FollowUpItem] = field(default_factory=list)
    checked: int = 0                   # Sentinel tasks looked at
    error: str | None = None

    @property
    def escalated(self) -> int:
        return sum(1 for i in self.items if i.kind == KIND_ESCALATION)

    @property
    def closed(self) -> int:
        return sum(1 for i in self.items if i.kind == KIND_OUTCOME)


class FollowUp:
    """Builds the follow-up items for one run; the run writes them to the log."""

    def __init__(self, repo: Any, snapshot: FarmSnapshot, run_id: str, cfg: Settings | None = None, notifier: Any = None) -> None:
        self.repo = repo
        self.snapshot = snapshot
        self.run_id = run_id
        self.cfg = cfg or default_settings
        self.notifier = notifier or FcmNotifier(self.cfg.google_cloud_project)
        self._manager: tuple[User | None, str | None] | None = None

    # ---------------------------------------------------------------- public
    def run(self) -> FollowUpReport:
        rep = FollowUpReport()
        now = self.repo.clock.now()
        window = timedelta(hours=self.cfg.escalation_hours)
        try:
            tasks = self.repo.get_sentinel_tasks(include_done=True)
        except Exception as exc:  # noqa: BLE001 — must not take the run down
            log.exception("follow-up could not list the Sentinel's tasks")
            rep.error = f"{type(exc).__name__}: {exc}"
            return rep
        for t in sorted(tasks, key=lambda t: (t.created_at or now)):
            rep.checked += 1
            try:
                if t.done:
                    if t.sentinel_outcome_logged_at is None:
                        rep.items.append(self._outcome(t, now))
                    continue
                last = t.escalated_at or t.created_at
                if last is not None and now - last >= window:
                    rep.items.append(self._escalate(t, now))
            except Exception as exc:  # noqa: BLE001
                log.exception("follow-up failed for task %s", t.id)
                rep.items.append(self._failed(t, now, exc))
        return rep

    # ------------------------------------------------------------ escalation
    def _escalate(self, t: Task, now: datetime) -> FollowUpItem:
        room_name = self._room_name(t)
        deaths_since, worklog = self._room_context(t, now)
        from_sev = t.severity or "medium"
        to_sev = next_severity(t.severity)
        open_for = (ago(t.created_at, now) or "an unknown time ago").replace(" ago", "")
        parts = [f"{worklog} work-log entr{'y' if worklog == 1 else 'ies'}" if worklog else "no work-log entries"]
        if t.signal_type == "MORTALITY_SPIKE" or deaths_since:
            parts.append(f"{deaths_since} more death{'s' if deaths_since != 1 else ''} in {room_name} since it was created")
        if t.escalation_count:
            parts.append(f"already escalated {t.escalation_count}×")
        detail = ", ".join(parts)
        if to_sev != from_sev:
            reason = (f"Still open {open_for} after it was created, past the {self.cfg.escalation_hours} h escalation window "
                      f"({detail}); severity raised {from_sev} → {to_sev}.")
        else:
            reason = (f"Still open {open_for} after it was created, past the {self.cfg.escalation_hours} h escalation window "
                      f"({detail}); already at the highest severity, the farm manager is nudged again.")

        # act: stamp + notify the farm manager
        action = ActionOutcome(type=KIND_ESCALATION, status=DONE, tool="escalate_task", task_id=t.id, task_title=t.title,
                               assignee_name=self._person(t.assignee_email))
        self.repo.escalate_task(t.id, to_sev, now)
        manager, note = self._farm_manager()
        if manager is None:
            action.status, action.reason = SKIPPED, note or "nobody to notify"
        else:
            title = f"Escalated ({to_sev}): {t.title}"
            body = reason
            status, why = self.notifier.send(manager, title, body, {
                "source": "sentinel", "kind": "escalation", "taskId": t.id, "roomName": room_name, "runId": self.run_id,
            })
            action.notifications.append(NotificationOutcome(to=f"{_name(manager)} (farm manager)", status=status, reason=why))
            if status != "sent":
                action.status = SKIPPED if status == SKIPPED else FAILED
                action.reason = why
        action.sent_at = now

        entry = escalation_entry(
            run_id=self.run_id, farm_name=self.snapshot.structure.farm.name, room_name=room_name, task=t, at=now,
            from_severity=from_sev, to_severity=to_sev, reason=reason,
            observation=(f"Sentinel task '{t.title}' ({t.signal_type.lower().replace('_', ' ') if t.signal_type else 'task'}, "
                         f"assigned to {self._person(t.assignee_email)}) has been open for {open_for}: {detail}."),
            action=action,
        )
        return FollowUpItem(KIND_ESCALATION, t, room_name, entry, action)

    # --------------------------------------------------------------- outcome
    def _outcome(self, t: Task, now: datetime) -> FollowUpItem:
        room_name = self._room_name(t)
        closed_at = t.completed_at or now
        deaths_since, worklog = self._room_context(t, closed_at)
        open_for = ago(t.created_at, closed_at).replace(" ago", "") if t.created_at else None
        parts = [f"closed {open_for} after it was created" if open_for else "closed"]
        parts.append(f"{worklog} work-log entr{'y' if worklog == 1 else 'ies'}" if worklog else "no work-log entries")
        if t.escalation_count:
            parts.append(f"escalated {t.escalation_count}× before it was closed")
        if t.signal_type == "MORTALITY_SPIKE" or deaths_since:
            parts.append(f"{deaths_since} death{'s' if deaths_since != 1 else ''} in {room_name} between creation and closing")
        observation = (f"Sentinel task '{t.title}' (assigned to {self._person(t.assignee_email)}, severity {t.severity or 'n/a'}) "
                       f"was {', '.join(parts)}.")
        self.repo.mark_task_outcome_logged(t.id, now)
        entry = outcome_entry(run_id=self.run_id, farm_name=self.snapshot.structure.farm.name, room_name=room_name, task=t,
                              at=now, observation=observation, closed_at=closed_at,
                              open_hours=round(max(0.0, (closed_at - t.created_at).total_seconds() / 3600), 1) if t.created_at else None,
                              deaths_since=deaths_since, work_log_entries=worklog)
        return FollowUpItem(KIND_OUTCOME, t, room_name, entry)

    def _failed(self, t: Task, now: datetime, exc: Exception) -> FollowUpItem:
        room_name = self._room_name(t)
        action = ActionOutcome(type=KIND_ESCALATION, status=FAILED, tool="escalate_task", task_id=t.id, task_title=t.title,
                               reason=f"{type(exc).__name__}: {exc}")
        entry = escalation_entry(run_id=self.run_id, farm_name=self.snapshot.structure.farm.name, room_name=room_name, task=t,
                                 at=now, from_severity=t.severity or "medium", to_severity=t.severity or "medium",
                                 reason=f"follow-up failed: {type(exc).__name__}: {exc}",
                                 observation=f"The follow-up of Sentinel task '{t.title}' failed.", action=action)
        entry["status"] = "failed"
        entry["error"] = action.reason
        return FollowUpItem(KIND_ESCALATION, t, room_name, entry, action)

    # -------------------------------------------------------------- helpers
    def _room_context(self, t: Task, until: datetime) -> tuple[int, int]:
        """(deaths in the task's room since the task was created, work-log entries)."""
        deaths = 0
        if t.terem_id and t.created_at:
            events = self.repo.get_mortality_events(t.created_at, until, t.terem_id)
            deaths = sum(e.darab for e in events)
        worklog = t.work_log_count if t.work_log_count is not None else self.repo.get_task_work_log_count(t.id)
        return deaths, worklog

    def _room_name(self, t: Task) -> str:
        room = self.snapshot.structure.room(t.terem_id) if t.terem_id else None
        if room is not None:
            return room.display_name
        return t.hizlalda_nev or self.snapshot.structure.farm.name

    def _person(self, email: str | None) -> str:
        if not email:
            return "nobody"
        u = self.snapshot.users_by_email.get(email.lower())
        return _name(u) if u else "a user outside the farm"

    def _farm_manager(self) -> tuple[User | None, str | None]:
        if self._manager is None:
            self._manager = self.repo.get_farm_manager()
        return self._manager


def _name(user: User) -> str:
    return user.display_name or (user.email.split("@")[0] if user.email else "unknown user")
