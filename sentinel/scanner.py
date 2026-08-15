"""Phase 1 — Scan. Deterministic, rule-based, no LLM.

The scanner answers one cheap question per rule: *is anything unusual enough
to look at?* It never decides what to do about it — that is the agent's job
in Phases 2–3. Most runs find nothing and the run ends here without spending a
single model token.

Design:

* :func:`load_snapshot` reads everything the rules need in one pass
  (structure, valves, today's mortality, today's valve changes, open tasks).
* :func:`scan` is a pure function ``(FarmSnapshot, Settings) -> [Candidate]``
  so it can be unit-tested against the seed plan without Firestore.
* Every :class:`Candidate` carries an English ``observation`` that only uses
  entity *names* — it is what the console shows under "Observed", and what
  the agent receives as its starting point.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings, SignalType, settings as default_settings
from .repository import PigOpsRepository, Structure
from .schema import MortalityEvent, Room, Task, User, Valve, ValveChange

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Data shapes
# --------------------------------------------------------------------------
@dataclass
class FarmSnapshot:
    """Everything Phase 1 needs, loaded once per run."""

    structure: Structure
    valves: dict[str, list[Valve]]                    # terem_id -> valves
    mortality_today: dict[str, list[MortalityEvent]]  # terem_id -> today's records
    valve_changes_today: dict[str, list[ValveChange]]  # terem_id -> today's changes
    open_tasks: list[Task]                            # work_log_count filled where relevant
    users_by_email: dict[str, User] = field(default_factory=dict)
    now: datetime | None = None
    tz: ZoneInfo = field(default_factory=lambda: default_settings.tz)


@dataclass
class Candidate:
    """Something worth a closer look. Not a decision."""

    signal_type: SignalType
    key: str                      # stable dedup key, e.g. "MORTALITY_SPIKE:h2-t5"
    room: Room | None             # None only for tasks that are not tied to a room
    room_name: str                # resolved display name (never an ID)
    observation: str              # English, with numbers, names only
    evidence: dict[str, Any]      # structured numbers for the reasoning phase
    task: Task | None = None      # DEADLINE_RISK only

    def as_log_fields(self) -> dict[str, Any]:
        return {"signalType": self.signal_type, "roomName": self.room_name, "observation": self.observation, "evidence": self.evidence}


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def load_snapshot(repo: PigOpsRepository, cfg: Settings | None = None) -> FarmSnapshot:
    cfg = cfg or default_settings
    structure = repo.get_structure()
    valves = repo.get_all_valves()
    mortality_today = repo.get_mortality_today_by_room()
    valve_changes_today = repo.get_valve_changes_today_by_room()
    open_tasks = repo.get_open_tasks()
    now = repo.clock.now()

    # Work-log lookups only for tasks that could become DEADLINE_RISK (overdue,
    # or due within the window) — keeps reads proportional to risk.
    for t in open_tasks:
        if t.deadline is not None and not t.is_sentinel_task:
            hours_left = (t.deadline - now).total_seconds() / 3600
            if hours_left <= cfg.deadline_soon_hours:
                t.work_log_count = repo.get_task_work_log_count(t.id)

    users_by_email: dict[str, User] = {}
    for p in repo.get_farm_permissions():
        u = repo.get_user(p.uid)
        if u and u.email:
            users_by_email[u.email.lower()] = u

    return FarmSnapshot(structure, valves, mortality_today, valve_changes_today, open_tasks, users_by_email, now, repo.clock.tz)


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------
def _fmt_hours(h: float) -> str:
    if h < 1:
        return f"{h * 60:.0f} min"
    if h < 48:
        return f"{h:.0f} h"
    return f"{h / 24:.1f} days"


def _person(snapshot: FarmSnapshot, email: str | None) -> str:
    if not email:
        return "nobody"
    u = snapshot.users_by_email.get(email.lower())
    return u.display_name if u and u.display_name else email


def rule_mortality_spike(snapshot: FarmSnapshot, cfg: Settings) -> list[Candidate]:
    out: list[Candidate] = []
    for room in snapshot.structure.rooms:
        events = snapshot.mortality_today.get(room.id, [])
        total = sum(e.darab for e in events)
        if total < cfg.mortality_spike_threshold:
            continue
        causes = Counter(e.ok or "unspecified" for e in events)
        # weight causes by head count, not by record count
        cause_heads: Counter[str] = Counter()
        for e in events:
            cause_heads[e.ok or "unspecified"] += e.darab
        cause_txt = ", ".join(f"{c} ×{n}" for c, n in cause_heads.most_common())
        pigs = sum(v.szelep for v in snapshot.valves.get(room.id, []))
        obs = (
            f"{total} deaths recorded today in {room.display_name} across {len(events)} "
            f"record{'s' if len(events) != 1 else ''} (causes: {cause_txt}); the room holds {pigs} pigs. "
            f"Threshold is {cfg.mortality_spike_threshold}."
        )
        out.append(
            Candidate(
                signal_type="MORTALITY_SPIKE",
                key=f"MORTALITY_SPIKE:{room.id}",
                room=room,
                room_name=room.display_name,
                observation=obs,
                evidence={
                    "deaths_today": total,
                    "records_today": len(events),
                    "causes_today": dict(cause_heads),
                    "pigs_in_room": pigs,
                    "threshold": cfg.mortality_spike_threshold,
                    "hizlalasi_nap": room.hizlalasi_nap,
                    "distinct_causes": len(causes),
                },
            )
        )
    return out


def rule_deadline_risk(snapshot: FarmSnapshot, cfg: Settings) -> list[Candidate]:
    out: list[Candidate] = []
    now = snapshot.now
    assert now is not None
    for t in snapshot.open_tasks:
        if t.done or t.deadline is None or t.is_sentinel_task:
            continue  # the Sentinel's own tasks are handled by the follow-up phase
        hours_left = (t.deadline - now).total_seconds() / 3600
        assignee = _person(snapshot, t.assignee_email)
        room = snapshot.structure.room(t.terem_id) if t.terem_id else None
        room_name = room.display_name if room else (t.hizlalda_nev or snapshot.structure.farm.name)
        if hours_left < 0:
            overdue_h = -hours_left
            touched = "" if t.work_log_count is None else (
                f" and has {t.work_log_count} work-log entr{'y' if t.work_log_count == 1 else 'ies'}"
                if t.work_log_count else " and has never been touched (no work-log entries)"
            )
            obs = (
                f"Task '{t.title}' assigned to {assignee} is {_fmt_hours(overdue_h)} overdue "
                f"(deadline {t.deadline.astimezone(snapshot.tz):%Y-%m-%d %H:%M} local){touched}."
            )
            kind = "overdue"
        elif hours_left <= cfg.deadline_soon_hours and not (t.work_log_count or 0):
            obs = (
                f"Task '{t.title}' assigned to {assignee} is due in {_fmt_hours(hours_left)} "
                f"and has never been touched (no work-log entries)."
            )
            kind = "due_soon_untouched"
        else:
            continue
        out.append(
            Candidate(
                signal_type="DEADLINE_RISK",
                key=f"DEADLINE_RISK:task:{t.id}",
                room=room,
                room_name=room_name,
                observation=obs,
                evidence={
                    "kind": kind,
                    "hours_left": round(hours_left, 1),
                    "task_title": t.title,
                    "assignee": assignee,
                    "work_log_entries": t.work_log_count,
                    "created_days_ago": round((now - t.created_at).total_seconds() / 86400, 1) if t.created_at else None,
                    "deadline_local": t.deadline.astimezone(snapshot.tz).isoformat(timespec="minutes"),
                },
                task=t,
            )
        )
    return out


def rule_silent_room(snapshot: FarmSnapshot, cfg: Settings) -> list[Candidate]:
    out: list[Candidate] = []
    now = snapshot.now
    assert now is not None
    for room in snapshot.structure.rooms:
        valves = snapshot.valves.get(room.id, [])
        pigs = sum(v.szelep for v in valves)
        if pigs <= 0:
            continue  # an empty room between batches is supposed to be quiet
        stamps = [v.last_modified for v in valves if v.last_modified is not None]
        stamps += [e.datum for e in snapshot.mortality_today.get(room.id, [])]
        stamps += [c.datum for c in snapshot.valve_changes_today.get(room.id, [])]
        last = max(stamps) if stamps else None
        hours = (now - last).total_seconds() / 3600 if last else None
        if hours is not None and hours <= cfg.silent_room_hours:
            continue
        if last is None:
            obs = (
                f"{room.display_name} holds {pigs} pigs but has never had any valve data written. "
                f"Threshold is {cfg.silent_room_hours} h without data."
            )
        else:
            obs = (
                f"{room.display_name} holds {pigs} pigs but no data has been written for {_fmt_hours(hours)} "
                f"(last activity {last.astimezone(snapshot.tz):%Y-%m-%d %H:%M} local). "
                f"Threshold is {cfg.silent_room_hours} h."
            )
        out.append(
            Candidate(
                signal_type="SILENT_ROOM",
                key=f"SILENT_ROOM:{room.id}",
                room=room,
                room_name=room.display_name,
                observation=obs,
                evidence={
                    "pigs_in_room": pigs,
                    "hours_silent": None if hours is None else round(hours, 1),
                    "last_activity_local": last.astimezone(snapshot.tz).isoformat(timespec="minutes") if last else None,
                    "threshold_hours": cfg.silent_room_hours,
                    "hizlalasi_nap": room.hizlalasi_nap,
                },
            )
        )
    return out


def rule_valve_instability(snapshot: FarmSnapshot, cfg: Settings) -> list[Candidate]:
    out: list[Candidate] = []
    for room in snapshot.structure.rooms:
        changes = snapshot.valve_changes_today.get(room.id, [])
        if not changes:
            continue
        per_valve: dict[str, list[ValveChange]] = {}
        for c in changes:
            per_valve.setdefault(c.szelep_name, []).append(c)
        worst_name, worst = max(per_valve.items(), key=lambda kv: len(kv[1]))
        room_total = len(changes)
        flap = len(worst) >= cfg.valve_flap_threshold
        busy = room_total >= cfg.valve_change_threshold
        if not (flap or busy):
            continue
        worst.sort(key=lambda c: c.datum)
        deltas = [c.modositas for c in worst]
        sign_flips = sum(1 for a, b in zip(deltas, deltas[1:]) if (a > 0) != (b > 0))
        net = sum(deltas)
        pattern = "/".join(f"{d:+d}" for d in deltas[:8]) + ("…" if len(deltas) > 8 else "")
        parts = []
        if flap:
            parts.append(
                f"Valve {worst_name} in {room.display_name} was modified {len(worst)} times today "
                f"({pattern}; {sign_flips} sign flips, net {net:+d}); per-valve threshold is {cfg.valve_flap_threshold}."
            )
        if busy:
            parts.append(
                f"{room.display_name} logged {room_total} valve modifications today across {len(per_valve)} valves; "
                f"room threshold is {cfg.valve_change_threshold}."
            )
        if flap and not busy:
            parts.append(f"Room total today: {room_total} changes across {len(per_valve)} valves.")
        out.append(
            Candidate(
                signal_type="VALVE_INSTABILITY",
                key=f"VALVE_INSTABILITY:{room.id}",
                room=room,
                room_name=room.display_name,
                observation=" ".join(parts),
                evidence={
                    "room_changes_today": room_total,
                    "valves_touched": len(per_valve),
                    "worst_valve": worst_name,
                    "worst_valve_changes": len(worst),
                    "worst_valve_deltas": deltas,
                    "sign_flips": sign_flips,
                    "net_delta_worst_valve": net,
                    "flap_threshold": cfg.valve_flap_threshold,
                    "room_threshold": cfg.valve_change_threshold,
                },
            )
        )
    return out


RULES = (rule_mortality_spike, rule_deadline_risk, rule_silent_room, rule_valve_instability)


def scan(snapshot: FarmSnapshot, cfg: Settings | None = None) -> list[Candidate]:
    """Run every rule; return candidates in a stable order (signal, room)."""
    cfg = cfg or default_settings
    found: list[Candidate] = []
    for rule in RULES:
        found.extend(rule(snapshot, cfg))
    found.sort(key=lambda c: (c.signal_type, c.room_name, c.key))
    return found


def partition_handled(
    candidates: list[Candidate],
    snapshot: FarmSnapshot,
    cfg: Settings | None = None,
    recent_decisions: list[dict[str, Any]] | None = None,
    sentinel_tasks: list[Task] | None = None,
) -> tuple[list[Candidate], list[tuple[Candidate, str]]]:
    """Split candidates into (fresh, already handled) — the idempotency guard (SPEC §8).

    A candidate is *handled* when the Sentinel already dealt with exactly it
    and that is still live:

    * an open Sentinel task for the same room and signal;
    * a Sentinel task for the same room and signal CLOSED inside the dedup
      window (``sentinel_tasks`` — the actor would refuse to re-open it anyway,
      so investigating would only spend tokens);
    * for DEADLINE_RISK — a notification about that task inside the dedup window;
    * a NOISE / WATCH decision on IDENTICAL evidence (same signal, room and
      observation text) inside ``DECISION_COOLDOWN_HOURS`` — ``recent_decisions``
      are sentinelLog documents from that window. If the evidence changes (one
      more death, another day of silence) the observation text changes and the
      candidate is investigated again.

    Handled candidates are counted on the run, not investigated again every
    15 minutes: that would only spend tokens to re-decide the same thing (the
    follow-up phase owns what happens to the Sentinel's tasks next).
    """
    cfg = cfg or default_settings
    now = snapshot.now
    assert now is not None
    window = timedelta(hours=cfg.dedup_window_hours)
    cooldown = timedelta(hours=cfg.decision_cooldown_hours)
    open_sentinel = [t for t in snapshot.open_tasks if t.is_sentinel_task and not t.done]
    closed_recent = [t for t in (sentinel_tasks or []) if t.done and t.created_at is not None and now - t.created_at < window]
    quiet: dict[tuple[str, str, str], tuple[str, datetime]] = {}
    for e in recent_decisions or []:
        ts = e.get("timestamp")
        if e.get("decision") in ("NOISE", "WATCH") and e.get("status") == "decided" and isinstance(ts, datetime):
            key = (e.get("signalType") or "", e.get("roomName") or "", e.get("observation") or "")
            if now - ts < cooldown and (key not in quiet or ts > quiet[key][1]):
                quiet[key] = (e["decision"], ts)
    fresh: list[Candidate] = []
    handled: list[tuple[Candidate, str]] = []
    for c in candidates:
        if c.signal_type == "DEADLINE_RISK":
            t = c.task
            if t is not None and t.sentinel_notified_at is not None and now - t.sentinel_notified_at < window:
                hours = (now - t.sentinel_notified_at).total_seconds() / 3600
                handled.append((c, f"already notified about this task {_fmt_hours(hours)} ago"))
                continue
        elif c.room is not None:
            match = next((t for t in open_sentinel if t.signal_type == c.signal_type and t.terem_id == c.room.id), None)
            if match is not None:
                handled.append((c, f"an open Sentinel task already covers it: '{match.title}'"))
                continue
            closed = next((t for t in closed_recent if t.signal_type == c.signal_type and t.terem_id == c.room.id), None)
            if closed is not None:
                when = closed.completed_at or closed.created_at
                hours = (now - when).total_seconds() / 3600 if when else 0.0
                handled.append((c, f"a Sentinel task for it was closed {_fmt_hours(max(hours, 0.0))} ago ('{closed.title}'); "
                                   f"not re-opened inside the {cfg.dedup_window_hours} h dedup window"))
                continue
        prior = quiet.get((c.signal_type, c.room_name, c.observation))
        if prior is not None:
            decision, ts = prior
            hours = (now - ts).total_seconds() / 3600
            handled.append((c, f"judged {decision} {_fmt_hours(hours)} ago on identical evidence; nothing has changed"))
            continue
        fresh.append(c)
    return fresh, handled


def run_scan(repo: PigOpsRepository, cfg: Settings | None = None) -> tuple[FarmSnapshot, list[Candidate]]:
    """Convenience: load + scan. Returns both so later phases can reuse the snapshot."""
    snap = load_snapshot(repo, cfg)
    cands = scan(snap, cfg)
    log.info("scan: %d candidate(s) on %s", len(cands), snap.structure.farm.name)
    return snap, cands


# --------------------------------------------------------------------------
# CLI: python -m sentinel.scanner [--json]
# --------------------------------------------------------------------------
def _main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    from .firestore_client import describe_target, get_db

    ap = argparse.ArgumentParser(prog="python -m sentinel.scanner", description="Run Phase 1 only and print the candidates.")
    ap.add_argument("--farm-id", default=default_settings.demo_farm_id)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    repo = PigOpsRepository(get_db(), args.farm_id)
    log.info("Target: %s", describe_target())
    snap, cands = run_scan(repo)
    if args.json:
        print(json.dumps([{**c.as_log_fields(), "key": c.key} for c in cands], indent=2, ensure_ascii=False, default=str))
    else:
        print(f"\n{snap.structure.farm.name}: {len(cands)} candidate(s)\n")
        for c in cands:
            print(f"[{c.signal_type}] {c.room_name}\n    {c.observation}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_main())
