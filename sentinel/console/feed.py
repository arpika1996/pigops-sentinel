"""The console's data side: what the page shows and how it stays live.

``ConsoleFeed`` keeps an in-memory picture of the last ``max_entries`` log
documents, today's run documents and the latest run, and polls Firestore
cheaply for changes:

* new log entries: ``timestamp >= last seen`` (an empty result costs one read)
* the latest run doc (one read) — its phase / currentTarget / counters change
  while a run is in progress, which is what makes the header show
  "investigating B-Telep / 5.Terem" as it happens

Every change becomes an event ``{"type": "log" | "run" | "stats", ...}``
broadcast to the SSE subscribers; ``state()`` is the full picture a page
loads first. Read-only by construction: it only calls ``get_*`` methods.

**Reads are demand-driven and shared.** Nothing polls Firestore in the
background: :meth:`ConsoleFeed.refresh_if_stale` runs a poll only when a
request arrives and the in-memory picture is older than the TTL, so ten
visitors cost the same as one, and nobody looking costs nothing at all.
The TTL is short while a run is in progress (the header is supposed to move)
and long when the farm is idle — which is most of the day.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ..config import Settings, settings as default_settings

log = logging.getLogger(__name__)

RUNNING = "running"

#: How long the in-memory picture may be served without touching Firestore.
#: Short while a run is in progress (the header is supposed to move), long
#: when the farm is idle — which is most of the day.
TTL_RUNNING_S = 2.0
TTL_IDLE_S = 15.0


def jsonable(value: Any) -> Any:
    """Recursively make a Firestore document JSON-safe (datetimes → ISO strings)."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


@dataclass
class ConsoleFeed:
    repo: Any
    cfg: Settings = field(default_factory=lambda: default_settings)
    max_entries: int = 200
    ttl_running_s: float = TTL_RUNNING_S
    ttl_idle_s: float = TTL_IDLE_S
    entries: list[dict[str, Any]] = field(default_factory=list)      # newest first, raw (datetimes kept)
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)     # today's runs + the latest, by id
    latest_run: dict[str, Any] | None = None
    last_seen: datetime | None = None
    _seen_ids: set[str] = field(default_factory=set)
    _day: date | None = None
    _subscribers: set[asyncio.Queue] = field(default_factory=set)
    _run_signature: tuple | None = None
    _refreshed_at: float | None = None                                # monotonic seconds of the last Firestore poll
    _refresh_lock: asyncio.Lock | None = None                         # one in-flight refresh, however many callers
    loaded: bool = False

    # ---------------------------------------------------------------- load
    def load_initial(self) -> None:
        """One-time full read: last N log entries, today's runs, latest run."""
        logs = self.repo.get_recent_logs(self.max_entries)
        self.entries = sorted(logs, key=lambda e: e.get("timestamp") or datetime.min.replace(tzinfo=None), reverse=True)
        self._seen_ids = {e["id"] for e in self.entries}
        if self.entries:
            self.last_seen = max(e["timestamp"] for e in self.entries if e.get("timestamp"))
        self._day = self.repo.clock.today()
        day_start, _ = self.repo.clock.day_bounds(self._day)
        self.runs = {r["id"]: r for r in self.repo.get_runs_since(day_start)}
        self.latest_run = self.repo.get_latest_run()
        if self.latest_run:
            self.runs.setdefault(self.latest_run["id"], self.latest_run)
        self._run_signature = _signature(self.latest_run)
        self.loaded = True

    # ---------------------------------------------------------------- poll
    def poll(self) -> list[dict[str, Any]]:
        """Fetch changes since the last poll; return the events to broadcast."""
        if not self.loaded:
            self.load_initial()
            return [{"type": "state", **self.state()}]
        events: list[dict[str, Any]] = []

        # midnight rollover: today's runs start from scratch
        today = self.repo.clock.today()
        if today != self._day:
            self._day = today
            day_start, _ = self.repo.clock.day_bounds(today)
            self.runs = {r["id"]: r for r in self.repo.get_runs_since(day_start)}

        # new log entries
        since = self.last_seen or (self.repo.clock.now() - timedelta(days=1))
        batch = self.repo.get_logs_since(since)
        # the world was reset (seed --reset-log) while this instance was alive: the newest entries we know
        # of no longer exist (or the runs are gone) → start over and tell every client to re-render
        newest_known = {e["id"] for e in self.entries if e.get("timestamp") == self.last_seen}
        vanished = bool(newest_known) and not (newest_known & {e["id"] for e in batch})
        if vanished or (self.repo.get_latest_run() is None and (self.latest_run is not None or self.entries)):
            return self._reload_after_reset()
        fresh = [e for e in batch if e["id"] not in self._seen_ids]
        for e in fresh:                                   # oldest first from the query
            self._seen_ids.add(e["id"])
            self.entries.insert(0, e)
            if e.get("timestamp") and (self.last_seen is None or e["timestamp"] > self.last_seen):
                self.last_seen = e["timestamp"]
            events.append({"type": "log", "entry": self.present(e)})
        if len(self.entries) > self.max_entries:
            for old in self.entries[self.max_entries:]:
                self._seen_ids.discard(old["id"])
            del self.entries[self.max_entries:]

        # the latest run (phase / counters change while it runs)
        latest = self.repo.get_latest_run()
        sig = _signature(latest)
        if latest and sig != self._run_signature:
            self._run_signature = sig
            self.latest_run = latest
            self.runs[latest["id"]] = latest
            events.append({"type": "run", "run": jsonable(latest)})

        if events:
            events.append({"type": "stats", "stats": self.stats()})
        return events

    def _reload_after_reset(self) -> list[dict[str, Any]]:
        self.entries, self._seen_ids, self.runs, self.latest_run, self.last_seen = [], set(), {}, None, None
        self._run_signature = None
        self.load_initial()
        log.info("log reset detected — reloaded: %d entries", len(self.entries))
        return [{"type": "state", **self.state()}]

    # ------------------------------------------------------------- caching
    def ttl_s(self) -> float:
        """Short while a run is in progress, long when the farm is idle."""
        running = bool(self.latest_run and self.latest_run.get("status") == RUNNING)
        return self.ttl_running_s if running else self.ttl_idle_s

    def is_stale(self) -> bool:
        return self._refreshed_at is None or (time.monotonic() - self._refreshed_at) >= self.ttl_s()

    def mark_fresh(self) -> None:
        self._refreshed_at = time.monotonic()

    async def refresh_if_stale(self) -> list[dict[str, Any]]:
        """Poll Firestore only if the in-memory picture aged past the TTL.

        This is what makes ten visitors cost the same as one: the poll runs at
        most once per TTL for the whole instance, under a lock so that a burst
        of requests produces a single read, and not at all while nobody asks.
        """
        if not self.is_stale():
            return []
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        async with self._refresh_lock:
            if not self.is_stale():                    # somebody refreshed while we waited
                return []
            try:
                events = await asyncio.to_thread(self.poll)
            except Exception:  # noqa: BLE001 — serve the last good picture rather than an error
                log.exception("console refresh failed; serving the cached state")
                self.mark_fresh()                      # do not hammer Firestore on a persistent failure
                return []
            self.mark_fresh()
            return events

    # --------------------------------------------------------------- state
    def state(self) -> dict[str, Any]:
        return {
            "farmName": self._farm_name(),
            "timezone": self.cfg.farm_timezone,
            "scanIntervalMinutes": self.cfg.scan_interval_minutes,
            "model": self.cfg.gemini_model,
            "run": jsonable(self.latest_run) if self.latest_run else None,
            "stats": self.stats(),
            "entries": [self.present(e) for e in self.entries],
        }

    def stats(self) -> dict[str, Any]:
        today = [r for r in self.runs.values() if self._is_today(r.get("startedAt"))]
        finished = [r for r in today if r.get("status") not in (RUNNING, None)]
        decisions = {"NOISE": 0, "WATCH": 0, "ACT": 0}
        for r in today:
            for k in decisions:
                decisions[k] += int((r.get("decisions") or {}).get(k, 0) or 0)
        last = self.latest_run
        return {
            "lastRunAt": jsonable(last.get("finishedAt") or last.get("startedAt")) if last else None,
            "lastRunStatus": last.get("status") if last else None,
            "runsToday": len(today),
            "finishedRunsToday": len(finished),
            "tasksCreatedToday": sum(int(r.get("tasksCreated") or 0) for r in today),
            "notificationsSentToday": sum(int(r.get("notificationsSent") or 0) for r in today),
            "escalationsToday": sum(int(r.get("escalated") or 0) for r in today),
            "candidatesToday": sum(int(r.get("candidates") or 0) for r in today),
            "handledToday": sum(int(r.get("handled") or 0) for r in today),
            "decisionsToday": decisions,
            "tokensToday": sum(int(r.get("tokens") or 0) for r in today),
            "entries": len(self.entries),
        }

    def present(self, e: dict[str, Any]) -> dict[str, Any]:
        """A log document as the page wants it (JSON-safe, plus a `kind` for the card)."""
        out = jsonable(e)
        status = e.get("status")
        if status == "escalated":
            kind = "escalation"
        elif status == "closed":
            kind = "outcome"
        elif status in ("failed", "run_failed"):
            kind = "failed"
        else:
            kind = (e.get("decision") or "unknown").lower()      # noise | watch | act
        out["kind"] = kind
        return out

    # ---------------------------------------------------------- subscribers
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def broadcast(self, events: list[dict[str, Any]]) -> None:
        for q in list(self._subscribers):
            for ev in events:
                try:
                    q.put_nowait(ev)
                except asyncio.QueueFull:              # a stalled client: drop it, not the others
                    self._subscribers.discard(q)
                    break

    async def run_forever(self, interval_s: float = 2.0) -> None:
        """Background task: poll → broadcast, forever; a poll error is logged and retried.

        Off by default — it reads Firestore whether or not anyone is watching,
        which is exactly the bill we did not want. Serving uses
        :meth:`refresh_if_stale` instead; this stays for local development
        (``--poller``) and for tests.
        """
        while True:
            try:
                events = await asyncio.to_thread(self.poll)
                self.mark_fresh()
                if events:
                    self.broadcast(events)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the console must survive a hiccup
                log.exception("console poll failed")
            await asyncio.sleep(interval_s)

    # -------------------------------------------------------------- helpers
    def _is_today(self, ts: datetime | None) -> bool:
        return ts is not None and self.repo.clock.local_day(ts) == self.repo.clock.today()

    def _farm_name(self) -> str:
        if self.latest_run and self.latest_run.get("farmName"):
            return self.latest_run["farmName"]
        for e in self.entries:
            if e.get("farmName"):
                return e["farmName"]
        return ""


def _signature(run: dict[str, Any] | None) -> tuple | None:
    if not run:
        return None
    return (run.get("id"), run.get("status"), run.get("phase"), run.get("currentTarget"), run.get("decided"),
            run.get("failed"), run.get("tasksCreated"), run.get("escalated"), run.get("closedOutcomes"),
            run.get("candidates"), run.get("handled"), run.get("lastLogId"), jsonable(run.get("finishedAt")))
