"""Read tools the agent can call while investigating a candidate.

Design rules (SPEC §4, §8):

* **Names in, names out.** The model addresses rooms by their display name
  (``"B-Telep / 5.Terem"``) exactly as ``get_structure`` returns them; the toolbox
  resolves names to documents internally. The model never sees a Firestore
  ID, so it cannot leak one into a task or the log.
* **Every call is traced.** ``ToolBox.trace`` records what was *actually*
  called, with arguments, duration and outcome — independent of what the
  model later claims in ``context_gathered``. Phase 4 logs the trace.
* **Fail loudly, but let the model recover.** A tool that raises returns
  ``{"error": ...}`` to the model *and* marks the trace entry as failed, so a
  failed look-up ends up in ``sentinelLog`` instead of vanishing.
* The agent decides which tools to call. Nothing here is sequenced.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from google.adk.tools import FunctionTool

from ..config import Settings, settings as default_settings
from ..scanner import FarmSnapshot
from ..schema import Room, Task

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Trace
# --------------------------------------------------------------------------
@dataclass
class ToolCall:
    tool: str
    args: dict[str, Any]
    ok: bool
    duration_ms: int
    summary: str                 # one line, human-readable, names only
    error: str | None = None

    def as_log(self) -> dict[str, Any]:
        d = {"tool": self.tool, "args": self.args, "ok": self.ok, "durationMs": self.duration_ms, "summary": self.summary}
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class ToolBox:
    """Builds the read tools over one farm snapshot + repository."""

    repo: Any                       # PigOpsRepository (or a test fake with the same reads)
    snapshot: FarmSnapshot
    cfg: Settings = field(default_factory=lambda: default_settings)
    trace: list[ToolCall] = field(default_factory=list)

    # ---------------------------------------------------------- resolution
    def _norm(self, s: str) -> str:
        """'B-Telep / 5.Terem' → 'b-telep5' — tolerant of case, spacing, dots, slashes and the word 'terem'."""
        return re.sub(r"terem|[\s\./]+", "", (s or "").lower())

    def _room(self, name: str) -> Room:
        rooms = self.snapshot.structure.rooms
        target = self._norm(name)
        for r in rooms:
            if self._norm(r.display_name) == target:
                return r
        # tolerate "5.Terem" alone if unique
        loose = [r for r in rooms if self._norm(r.name) == target]
        if len(loose) == 1:
            return loose[0]
        known = ", ".join(r.display_name for r in rooms)
        raise LookupError(f"Unknown room {name!r}. Use one of: {known}")

    def _person(self, email: str | None) -> str:
        if not email:
            return "unassigned"
        u = self.snapshot.users_by_email.get(email.lower())
        return u.display_name if u and u.display_name else email

    def _local(self, dt) -> str | None:
        return dt.astimezone(self.snapshot.tz).strftime("%Y-%m-%d %H:%M") if dt else None

    def _pigs(self, room: Room) -> int:
        return sum(v.szelep for v in self.snapshot.valves.get(room.id, []))

    def _task_view(self, t: Task) -> dict[str, Any]:
        now = self.snapshot.now
        if t.work_log_count is None and hasattr(self.repo, "get_task_work_log_count"):
            try:
                t.work_log_count = self.repo.get_task_work_log_count(t.id)
            except Exception:  # pragma: no cover - defensive
                t.work_log_count = None
        room = self.snapshot.structure.room(t.terem_id) if t.terem_id else None
        return {
            "title": t.title,
            "description": t.description,
            "room": room.display_name if room else (t.hizlalda_nev or "farm-wide"),
            "assignee": self._person(t.assignee_email),
            "deadline_local": self._local(t.deadline),
            "hours_left": round((t.deadline - now).total_seconds() / 3600, 1) if t.deadline else None,
            "created_days_ago": round((now - t.created_at).total_seconds() / 86400, 1) if t.created_at else None,
            "work_log_entries": t.work_log_count,
            "created_by_sentinel": t.is_sentinel_task,
            "severity": t.severity,
            "status": "done" if t.done else "open",
        }

    # ------------------------------------------------------------- wrapping
    def _wrap(self, name: str, fn: Callable[..., dict[str, Any]], summarise: Callable[[dict[str, Any]], str]) -> Callable[..., dict[str, Any]]:
        def run(**kwargs: Any) -> dict[str, Any]:
            t0 = time.perf_counter()
            try:
                out = fn(**kwargs)
                self.trace.append(ToolCall(name, kwargs, True, int((time.perf_counter() - t0) * 1000), summarise(out)))
                return out
            except Exception as exc:  # noqa: BLE001 — surfaced to the model and the log
                msg = f"{type(exc).__name__}: {exc}"
                log.warning("tool %s%s failed: %s", name, kwargs, msg)
                self.trace.append(ToolCall(name, kwargs, False, int((time.perf_counter() - t0) * 1000), "failed", msg))
                return {"error": msg}

        return run

    # ---------------------------------------------------------------- tools
    def tools(self) -> list[FunctionTool]:
        """The FunctionTools handed to the LlmAgent."""
        box = self

        def get_structure() -> dict:
            """Farm → barns → rooms with current pig counts. Call this first if you
            need to know which rooms exist or how big the farm is. Rooms are always
            referred to by their display name, e.g. "B-Telep / 5.Terem"."""
            st = box.snapshot.structure
            barns = []
            for b in st.barns:
                rooms = []
                for r in st.rooms_of(b):
                    rooms.append({
                        "room": r.display_name,
                        "pigs": box._pigs(r),
                        "fattening_day": r.hizlalasi_nap,
                        "estimated_weight_kg": r.becsult_suly,
                        "open_room_problems": r.problem_count,
                    })
                barns.append({"barn": b.name, "rooms": rooms})
            return {"farm": st.farm.name, "barns": barns, "total_pigs": sum(box._pigs(r) for r in st.rooms)}

        def get_room_stats(room: str) -> dict:
            """Current state of one room: pig count, fattening day, estimated weight,
            valve count and faults, deaths and valve changes recorded today, and how
            long ago the last data was written. `room` is the display name, e.g.
            "A-Telep / 3.Terem"."""
            r = box._room(room)
            valves = box.snapshot.valves.get(r.id, [])
            deaths = box.snapshot.mortality_today.get(r.id, [])
            changes = box.snapshot.valve_changes_today.get(r.id, [])
            stamps = [v.last_modified for v in valves if v.last_modified] + [e.datum for e in deaths] + [c.datum for c in changes]
            last = max(stamps) if stamps else None
            return {
                "room": r.display_name,
                "pigs": sum(v.szelep for v in valves),
                "valves": len(valves),
                "empty_valves": sum(1 for v in valves if v.szelep == 0),
                "fattening_day": r.hizlalasi_nap,
                "estimated_weight_kg": r.becsult_suly,
                "open_room_problems": r.problem_count,
                "valve_faults": sum(1 for v in valves if v.hiba_allapot),
                "deaths_today": sum(e.darab for e in deaths),
                "death_causes_today": dict(Counter(e.ok or "unspecified" for e in deaths)),
                "valve_changes_today": len(changes),
                "last_data_written_local": box._local(last),
                "hours_since_last_data": round((box.snapshot.now - last).total_seconds() / 3600, 1) if last else None,
            }

        def get_mortality_history(room: str, days: int = 14) -> dict:
            """Daily death counts for one room over the last `days` days (today
            included, oldest first) with the mean of the previous days for
            comparison. Use it to tell a one-off spike from a chronic problem."""
            r = box._room(room)
            days = max(2, min(int(days), 60))
            series = box.repo.get_mortality_history(r.id, days)
            prev = series[:-1]
            prev_total = sum(d.count for d in prev)
            today = series[-1].count if series else 0
            nonzero_days = sum(1 for d in prev if d.count > 0)
            return {
                "room": r.display_name,
                "days": days,
                "series": [{"date": d.day.isoformat(), "deaths": d.count} for d in series],
                "today": today,
                "previous_days_total": prev_total,
                "previous_days_mean": round(prev_total / len(prev), 2) if prev else None,
                "previous_days_max": max((d.count for d in prev), default=0),
                "previous_days_with_deaths": nonzero_days,
                "last_3_days": [d.count for d in series[-4:-1]],
                "pigs_now": box._pigs(r),
            }

        def get_farm_mortality_overview(days: int = 7) -> dict:
            """Deaths per room across the whole farm for the last `days` days plus
            today, so you can see whether a problem is confined to one room or
            spreading through a barn."""
            days = max(1, min(int(days), 60))
            first_day = box.snapshot.now.astimezone(box.snapshot.tz).date() - timedelta(days=days - 1)
            since = box.repo.clock.day_bounds(first_day)[0]
            events = box.repo.get_mortality_events(since)
            per_room: dict[str, int] = defaultdict(int)
            per_room_today: dict[str, int] = defaultdict(int)
            today = box.repo.clock.today()
            for e in events:
                per_room[e.room.terem_id] += e.darab
                if box.repo.clock.local_day(e.datum) == today:
                    per_room_today[e.room.terem_id] += e.darab
            rows = []
            for r in box.snapshot.structure.rooms:
                pigs = box._pigs(r)
                if pigs == 0 and per_room.get(r.id, 0) == 0:
                    continue
                rows.append({
                    "room": r.display_name,
                    "pigs": pigs,
                    f"deaths_last_{days}_days": per_room.get(r.id, 0),
                    "deaths_today": per_room_today.get(r.id, 0),
                    "mean_per_day": round(per_room.get(r.id, 0) / days, 2),
                })
            return {"days": days, "farm": box.snapshot.structure.farm.name, "rooms": rows,
                    "farm_total": sum(per_room.values()), "farm_today": sum(per_room_today.values())}

        def get_open_tasks(room: str = "") -> dict:
            """Open tasks (faults, jobs) — for one room if `room` is given, otherwise
            for the whole farm. Shows assignee, deadline, how many work-log entries
            exist (0 = nobody has touched it) and whether the Sentinel created it."""
            r = box._room(room) if room else None
            tasks = [t for t in box.snapshot.open_tasks if not r or t.terem_id == r.id]
            return {"room": r.display_name if r else "whole farm", "count": len(tasks), "tasks": [box._task_view(t) for t in tasks]}

        def get_valve_log(room: str, days: int = 1) -> dict:
            """Valve modification log for one room over the last `days` days: per-valve
            change counts, the sequence of deltas (to spot back-and-forth edits) and
            the most recent entries. Valve numbers encode the room (room 4 → 401-416)."""
            r = box._room(room)
            days = max(1, min(int(days), 30))
            entries = box.repo.get_valve_log(r.id, days)
            per_valve: dict[str, list[int]] = defaultdict(list)
            for c in entries:
                per_valve[c.szelep_name].append(c.modositas)
            valves = []
            for name, deltas in sorted(per_valve.items(), key=lambda kv: -len(kv[1])):
                flips = sum(1 for a, b in zip(deltas, deltas[1:]) if (a > 0) != (b > 0))
                valves.append({"valve": name, "changes": len(deltas), "deltas": deltas, "net": sum(deltas), "sign_flips": flips})
            recent = [
                {"time_local": box._local(c.datum), "valve": c.szelep_name, "delta": c.modositas, "set_absolute": c.abszolut}
                for c in entries[-25:]
            ]
            return {"room": r.display_name, "days": days, "total_changes": len(entries), "valves_touched": len(per_valve),
                    "per_valve": valves[:10], "recent_entries": recent}

        wrapped = [
            self._wrap("get_structure", get_structure, lambda o: f"{o['farm']}: {sum(len(b['rooms']) for b in o['barns'])} rooms, {o['total_pigs']} pigs"),
            self._wrap("get_room_stats", get_room_stats, lambda o: f"{o['room']}: {o['pigs']} pigs, {o['deaths_today']} deaths today, last data {o['hours_since_last_data']} h ago"),
            self._wrap("get_mortality_history", get_mortality_history, lambda o: f"{o['room']}: today {o['today']} vs previous-days mean {o['previous_days_mean']} over {o['days']} days"),
            self._wrap("get_farm_mortality_overview", get_farm_mortality_overview, lambda o: f"farm: {o['farm_today']} deaths today, {o['farm_total']} in {o['days']} days across {len(o['rooms'])} rooms"),
            self._wrap("get_open_tasks", get_open_tasks, lambda o: f"{o['room']}: {o['count']} open task(s)"),
            self._wrap("get_valve_log", get_valve_log, lambda o: f"{o['room']}: {o['total_changes']} changes on {o['valves_touched']} valves in {o['days']} day(s)"),
        ]
        # FunctionTool reads name/docstring/signature from the callable — copy them from the originals
        originals = [get_structure, get_room_stats, get_mortality_history, get_farm_mortality_overview, get_open_tasks, get_valve_log]
        tools: list[FunctionTool] = []
        for w, o in zip(wrapped, originals):
            w.__name__ = o.__name__
            w.__doc__ = o.__doc__
            w.__signature__ = _signature_of(o)  # type: ignore[attr-defined]
            w.__annotations__ = dict(o.__annotations__)
            tools.append(FunctionTool(w))
        return tools


def _signature_of(fn: Callable[..., Any]):
    import inspect

    return inspect.signature(fn)
