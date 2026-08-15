"""Phase 4 — Act: the two action tools and the guardrails around them.

The model *proposes* (``Decision.action_title/body``); this module *acts*, and
every guardrail from SPEC §4 lives here in plain code, not in the prompt:

* ``create_task``  — MORTALITY_SPIKE / SILENT_ROOM / VALVE_INSTABILITY: a new
  task for the person responsible for the farm, who is then notified.
  Refused when an equivalent Sentinel task exists inside the dedup window
  (``deduplicated``) or the per-run cap is reached (``capped``).
* ``send_notification`` — DEADLINE_RISK: the task already exists, so the
  assignee and the farm manager are notified about it. Refused when the
  Sentinel already notified about that task inside the dedup window.

Assignee resolution: the farm-level admin from the farm's permission
subcollection; if none, a superadmin / global admin with access to the farm,
and the fallback is *noted in the log*; if nobody fits, the task is created
unassigned and that is noted too (SPEC §4).

Notifications go through FCM (firebase-admin). The demo farm has no device
tokens on purpose, so they are recorded as ``skipped`` with the reason — the
log shows the agent *tried*, which is the point of the log.

Whatever happens here ends up in ``sentinelLog.actionTaken``; an exception
inside an action becomes a ``failed`` outcome, never a crashed run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .agent.investigator import InvestigationResult
from .agent.schemas import Decision
from .config import Settings, settings as default_settings
from .logbook import action_kind
from .scanner import Candidate, FarmSnapshot
from .schema import User

log = logging.getLogger(__name__)

# outcome.status
DONE = "done"                  # the action was carried out
DEDUPLICATED = "deduplicated"  # an equivalent action already exists inside the dedup window
CAPPED = "capped"              # per-run task cap reached
SKIPPED = "skipped"            # nothing could be done (e.g. nobody to notify, no device tokens)
FAILED = "failed"              # an error; details in reason

# notification.status
SENT = "sent"


@dataclass
class NotificationOutcome:
    to: str                    # display name — never a uid/email
    status: str                # sent | skipped | failed
    reason: str | None = None

    def as_log(self) -> dict[str, Any]:
        d: dict[str, Any] = {"to": self.to, "status": self.status}
        if self.reason:
            d["reason"] = self.reason
        return d


@dataclass
class ActionOutcome:
    """What Phase 4 did for one ACT decision → ``sentinelLog.actionTaken`` (SPEC §5)."""

    type: str                              # task | notification
    status: str                            # done | deduplicated | capped | skipped | failed
    tool: str                              # create_task | send_notification
    task_id: str | None = None
    task_title: str | None = None
    assignee_name: str | None = None
    assignee_note: str | None = None       # fallback explanation (SPEC §4)
    deadline: datetime | None = None
    sent_at: datetime | None = None
    notifications: list[NotificationOutcome] = field(default_factory=list)
    reason: str | None = None

    @property
    def notifications_sent(self) -> int:
        return sum(1 for n in self.notifications if n.status == SENT)

    def as_log(self, json_safe: bool = False) -> dict[str, Any]:
        """The ``actionTaken`` object; ``json_safe`` renders datetimes as ISO strings (CLI --json)."""
        d: dict[str, Any] = {"type": self.type, "status": self.status, "tool": self.tool}
        for key, val in (("taskId", self.task_id), ("taskTitle", self.task_title), ("assigneeName", self.assignee_name),
                         ("assigneeNote", self.assignee_note), ("deadline", self.deadline), ("sentAt", self.sent_at),
                         ("reason", self.reason)):
            if val is not None:
                d[key] = val.isoformat() if json_safe and isinstance(val, datetime) else val
        if self.notifications:
            d["notifications"] = [n.as_log() for n in self.notifications]
        return d


# --------------------------------------------------------------------------
# Notification transport
# --------------------------------------------------------------------------
class FcmNotifier:
    """Sends one push notification per user via Firebase Cloud Messaging."""

    def __init__(self, project_id: str | None = None) -> None:
        self.project_id = project_id
        self._app = None

    def _app_handle(self):
        if self._app is None:
            import firebase_admin  # noqa: PLC0415 — keep the import out of the test path

            try:
                self._app = firebase_admin.get_app()
            except ValueError:
                options = {"projectId": self.project_id} if self.project_id else None
                self._app = firebase_admin.initialize_app(options=options)
        return self._app

    def send(self, user: User, title: str, body: str, data: dict[str, str]) -> tuple[str, str | None]:
        """Returns (status, reason). Never raises."""
        name = _name(user)
        if not user.fcm_token:
            return SKIPPED, f"no device token registered for {name}"
        try:
            from firebase_admin import messaging  # noqa: PLC0415

            msg = messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                data={k: str(v) for k, v in data.items()},
                token=user.fcm_token,
            )
            messaging.send(msg, app=self._app_handle())
            return SENT, None
        except Exception as exc:  # noqa: BLE001 — recorded, not raised
            return FAILED, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# The actor
# --------------------------------------------------------------------------
class Actor:
    """Executes ACT decisions for one run, with the guardrails."""

    def __init__(self, repo: Any, snapshot: FarmSnapshot, run_id: str, cfg: Settings | None = None, notifier: Any = None) -> None:
        self.repo = repo
        self.snapshot = snapshot
        self.run_id = run_id
        self.cfg = cfg or default_settings
        self.notifier = notifier or FcmNotifier(self.cfg.google_cloud_project)
        self.tasks_created = 0
        self.notifications_sent = 0
        self.notifications_skipped = 0
        self._manager: tuple[User | None, str | None] | None = None

    # ------------------------------------------------------------ dispatch
    def act(self, result: InvestigationResult) -> ActionOutcome | None:
        """None when there is nothing to do (not ACT / no decision)."""
        d = result.decision
        if d is None or d.decision != "ACT":
            return None
        c = result.candidate
        kind = action_kind(c.signal_type)
        try:
            outcome = self._notify_about_task(c, d) if kind == "notification" else self._create_task(c, d)
        except Exception as exc:  # noqa: BLE001 — an action error must not take the run down
            log.exception("action failed for %s", c.key)
            outcome = ActionOutcome(type=kind, status=FAILED, tool="send_notification" if kind == "notification" else "create_task",
                                    reason=f"{type(exc).__name__}: {exc}")
        self.notifications_sent += outcome.notifications_sent
        self.notifications_skipped += sum(1 for n in outcome.notifications if n.status == SKIPPED)
        return outcome

    # ----------------------------------------------------------- create_task
    def _create_task(self, c: Candidate, d: Decision) -> ActionOutcome:
        out = ActionOutcome(type="task", status=DONE, tool="create_task", task_title=d.action_title)
        if c.room is None:
            out.status, out.reason = FAILED, "the candidate is not tied to a room"
            return out

        existing = self.repo.find_recent_sentinel_task(c.room.id, c.signal_type, self.cfg.dedup_window_hours)
        if existing is not None:
            out.status = DEDUPLICATED
            out.task_id, out.task_title = existing.id, existing.title
            ago = _ago(existing.created_at, self.repo.clock.now())
            out.reason = (f"an {'open' if not existing.done else 'recently closed'} Sentinel task for {_signal_label(c.signal_type)} "
                          f"in {c.room_name} already exists{f' (created {ago})' if ago else ''}: '{existing.title}'")
            return out

        if self.tasks_created >= self.cfg.max_tasks_per_run:
            out.status = CAPPED
            out.reason = f"the cap of {self.cfg.max_tasks_per_run} new tasks per run is reached; not creating another one"
            return out

        user, note = self._farm_manager()
        deadline = self._deadline_for(d)
        task = self.repo.create_task(
            room=c.room, title=d.action_title or "", description=d.action_body or "",
            assignee_email=user.email if user else None, severity=d.severity or "medium",
            signal_type=c.signal_type, run_id=self.run_id, deadline=deadline, signal_key=c.key,
        )
        self.tasks_created += 1
        out.task_id, out.task_title, out.deadline = task.id, task.title, deadline
        out.assignee_name, out.assignee_note = (_name(user) if user else None), note
        out.sent_at = self.repo.clock.now()
        if user is not None:
            out.notifications.append(self._send(user, d.action_title or "", d.action_body or "", c, task_id=task.id))
        return out

    # ------------------------------------------------------ send_notification
    def _notify_about_task(self, c: Candidate, d: Decision) -> ActionOutcome:
        out = ActionOutcome(type="notification", status=DONE, tool="send_notification", task_title=d.action_title)
        task = c.task
        if task is None:
            out.status, out.reason = FAILED, "the candidate carries no task to notify about"
            return out
        now = self.repo.clock.now()
        out.task_id, out.task_title = task.id, task.title

        window = timedelta(hours=self.cfg.dedup_window_hours)
        if task.sentinel_notified_at is not None and now - task.sentinel_notified_at < window:
            out.status = DEDUPLICATED
            out.reason = f"the Sentinel already notified about this task {_ago(task.sentinel_notified_at, now)}; not repeating it"
            return out

        recipients: list[tuple[User, str]] = []
        assignee = self.snapshot.users_by_email.get((task.assignee_email or "").lower()) if task.assignee_email else None
        if assignee is not None:
            recipients.append((assignee, "assignee"))
            out.assignee_name = _name(assignee)
        elif task.assignee_email:
            out.assignee_note = "the task's assignee is not among the farm's users; notifying the farm manager only"
        else:
            out.assignee_note = "the task has no assignee; notifying the farm manager only"
        manager, note = self._farm_manager()
        if manager is not None and all(manager.uid != u.uid for u, _ in recipients):
            recipients.append((manager, "farm manager"))
        if not recipients:
            out.status = SKIPPED
            out.reason = note or "nobody to notify"
            return out

        for user, role in recipients:
            n = self._send(user, d.action_title or "", d.action_body or "", c, task_id=task.id)
            n.to = f"{n.to} ({role})"
            out.notifications.append(n)
        out.sent_at = now

        statuses = {n.status for n in out.notifications}
        if statuses == {FAILED}:
            out.status, out.reason = FAILED, "every notification failed"      # not marked → retried next run
            return out
        if SENT not in statuses:
            out.status, out.reason = SKIPPED, "no device tokens — nobody could be reached"
        # sent, or nothing to retry: stamp the task so the next runs do not nag
        self.repo.mark_task_notified(task.id, now)
        return out

    # -------------------------------------------------------------- helpers
    def _send(self, user: User, title: str, body: str, c: Candidate, task_id: str | None) -> NotificationOutcome:
        data = {"source": "sentinel", "signalType": c.signal_type, "roomName": c.room_name, "runId": self.run_id}
        if task_id:
            data["taskId"] = task_id
        status, reason = self.notifier.send(user, title, body, data)
        return NotificationOutcome(to=_name(user), status=status, reason=reason)

    def _farm_manager(self) -> tuple[User | None, str | None]:
        if self._manager is None:
            self._manager = self.repo.get_farm_manager()
        return self._manager

    def _deadline_for(self, d: Decision) -> datetime:
        """'Needs a person today' → the deadline is the end of the farm's day; high severity gets 4 hours."""
        clock = self.repo.clock
        end_of_day = clock.day_bounds(clock.today())[1]
        if d.severity == "high":
            return min(end_of_day, clock.now() + timedelta(hours=4))
        return end_of_day


# --------------------------------------------------------------------------
def _name(user: User) -> str:
    return user.display_name or (user.email.split("@")[0] if user.email else "unknown user")


def _signal_label(signal_type: str) -> str:
    return signal_type.replace("_", " ").lower()


def _ago(then: datetime | None, now: datetime) -> str:
    """'32 min ago' / '5 h ago' / '3 days ago' — '' when unknown."""
    if then is None:
        return ""
    hours = (now - then).total_seconds() / 3600
    if hours < 1:
        return f"{int(hours * 60)} min ago"
    if hours < 48:
        return f"{hours:.0f} h ago"
    return f"{hours / 24:.0f} days ago"
