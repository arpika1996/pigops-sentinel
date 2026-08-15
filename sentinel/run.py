"""One Sentinel run, end to end: scan → investigate → decide → log.

    python -m sentinel.run                # a real run: writes sentinelLog + sentinelRuns
    python -m sentinel.run --json
    python -m sentinel.run --trigger scheduler   (what the Cloud Run endpoint passes)

This is the orchestrator the Cloud Run service (step 8) calls on every tick.
What it guarantees:

* **Every candidate leaves a log entry** — decided or failed — written the
  moment its investigation ends, so the console can show the run unfolding.
* **The run itself is a document** (``sentinelRuns/{runId}``): live phase
  (scanning → investigating <room> → done), counters, tokens, error. The
  console header ("last run", "runs today") reads this.
* **Overlapping runs are refused**, not doubled: if a run is still marked
  running and started less than two scan intervals ago, this one records
  itself as ``skipped`` and exits (SPEC §8: idempotent runs).
* **Failures are loud**: a broken scan or investigation ends in a ``failed``
  run doc plus a run-level log entry, never a silent exit.

Phase 4 (task creation / notification) and Phase 5 (follow-up) hook in
between "decided" and "logged" in the next steps; the log entry already
carries ``proposedAction`` for them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from .agent.investigator import InvestigationResult, Investigator, StatusCallback
from .config import Settings, settings as default_settings
from .logbook import build_log_entry, run_failure_entry
from .scanner import Candidate, FarmSnapshot, load_snapshot, scan

log = logging.getLogger(__name__)

# run status (sentinelRuns.status)
RUN_RUNNING = "running"
RUN_OK = "ok"            # every candidate decided (also: nothing to investigate)
RUN_PARTIAL = "partial"  # some investigations failed, the rest were logged
RUN_FAILED = "failed"    # the run broke, or every investigation failed
RUN_SKIPPED = "skipped"  # refused because another run is still in progress

# run phase (sentinelRuns.phase)
PHASE_SCANNING = "scanning"
PHASE_INVESTIGATING = "investigating"
PHASE_DONE = "done"

Loader = Callable[[Any, Settings], FarmSnapshot]
InvestigatorFactory = Callable[..., Any]   # (repo, snapshot, run_id, cfg, status) -> Investigator-like


@dataclass
class RunReport:
    """What one run did — returned to the caller and mirrored into sentinelRuns."""

    run_id: str
    trigger: str
    started_at: datetime
    finished_at: datetime | None = None
    farm_name: str = ""
    status: str = RUN_RUNNING
    candidates: list[Candidate] = field(default_factory=list)
    results: list[InvestigationResult] = field(default_factory=list)
    log_ids: list[str] = field(default_factory=list)
    error: str | None = None
    skip_reason: str | None = None

    @property
    def decided(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    @property
    def decisions(self) -> Counter:
        return Counter(r.decision.decision for r in self.results if r.decision)

    @property
    def total_tokens(self) -> int:
        return sum(r.total_tokens for r in self.results)

    @property
    def latency_ms(self) -> int:
        if self.finished_at is None:
            return 0
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    def as_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id, "trigger": self.trigger, "status": self.status, "farmName": self.farm_name,
            "startedAt": self.started_at.isoformat(), "finishedAt": self.finished_at.isoformat() if self.finished_at else None,
            "candidates": len(self.candidates), "decided": self.decided, "failed": self.failed,
            "decisions": dict(self.decisions), "tokens": self.total_tokens, "latencyMs": self.latency_ms,
            "logIds": list(self.log_ids), "error": self.error, "skipReason": self.skip_reason,
        }


def new_run_id(now: datetime) -> str:
    """Sortable and readable ("20260815-112704-3f9a1c", farm-local time); carries no farm IDs."""
    return f"{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


class SentinelRun:
    """Runs the loop once. Construct, then ``execute()`` (sync) or ``await execute_async()``."""

    def __init__(
        self,
        repo: Any,
        cfg: Settings | None = None,
        *,
        trigger: str = "manual",
        status: StatusCallback | None = None,
        load: Loader = load_snapshot,
        investigator_factory: InvestigatorFactory = Investigator,
    ) -> None:
        self.repo = repo
        self.cfg = cfg or default_settings
        self.trigger = trigger
        self.status = status or (lambda phase, detail: None)
        self.load = load
        self.investigator_factory = investigator_factory
        now = repo.clock.now()
        # the id reads in farm-local time ("20260815-112704-…"), the timestamps stay UTC-aware
        self.report = RunReport(run_id=new_run_id(repo.clock.now_local()), trigger=trigger, started_at=now)

    # ---------------------------------------------------------------- public
    def execute(self) -> RunReport:
        return asyncio.run(self.execute_async())

    async def execute_async(self) -> RunReport:
        rep = self.report
        if (reason := self._overlapping_run()) is not None:
            rep.status, rep.skip_reason, rep.finished_at = RUN_SKIPPED, reason, self.repo.clock.now()
            self._upsert(status=RUN_SKIPPED, phase=PHASE_DONE, skipReason=reason, finishedAt=rep.finished_at)
            self.status("skipped", reason)
            log.warning("run %s skipped: %s", rep.run_id, reason)
            return rep

        self._upsert(status=RUN_RUNNING, phase=PHASE_SCANNING, currentTarget=None, trigger=self.trigger,
                     model=self.cfg.gemini_model, candidates=0, decided=0, failed=0,
                     decisions={"NOISE": 0, "WATCH": 0, "ACT": 0}, tasksCreated=0, notificationsSent=0, error=None)

        # ---- Phase 1: scan (deterministic) --------------------------------
        self.status(PHASE_SCANNING, "loading the farm snapshot")
        try:
            snapshot = self.load(self.repo, self.cfg)
            rep.farm_name = snapshot.structure.farm.name
            rep.candidates = scan(snapshot, self.cfg)
        except Exception as exc:  # noqa: BLE001 — must end in the log, never vanish
            return self._fail(PHASE_SCANNING, exc)
        self._upsert(farmName=rep.farm_name, candidates=len(rep.candidates))
        self.status(PHASE_SCANNING, f"{len(rep.candidates)} candidate(s)")

        # ---- Phase 2+3: investigate + decide, one candidate at a time -----
        if rep.candidates:
            inv = None
            try:
                inv = self.investigator_factory(self.repo, snapshot, rep.run_id, self.cfg, self.status)
                for c in rep.candidates:
                    self._upsert(phase=PHASE_INVESTIGATING, currentTarget=f"{c.signal_type} — {c.room_name}")
                    result = await inv.investigate(c)
                    rep.results.append(result)
                    self._log_result(result)
            except Exception as exc:  # noqa: BLE001
                return self._fail(PHASE_INVESTIGATING, exc)
            finally:
                if inv is not None:
                    await inv.close()

        # ---- wrap up ------------------------------------------------------
        rep.finished_at = self.repo.clock.now()
        if rep.results and rep.decided == 0:
            rep.status = RUN_FAILED
            rep.error = "every investigation failed"
        elif rep.failed:
            rep.status = RUN_PARTIAL
        else:
            rep.status = RUN_OK
        self._upsert(status=rep.status, phase=PHASE_DONE, currentTarget=None, finishedAt=rep.finished_at,
                     latencyMs=rep.latency_ms, tokens=rep.total_tokens, error=rep.error)
        self.status(PHASE_DONE, f"{rep.status}: {rep.decided} decided, {rep.failed} failed")
        log.info("run %s %s: %d candidate(s), %d decided, %d failed", rep.run_id, rep.status, len(rep.candidates), rep.decided, rep.failed)
        return rep

    # -------------------------------------------------------------- helpers
    def _overlapping_run(self) -> str | None:
        """Another run still marked running and younger than two scan intervals → refuse."""
        window = timedelta(minutes=2 * self.cfg.scan_interval_minutes)
        now = self.report.started_at
        try:
            recent = self.repo.get_recent_runs(5)
        except Exception as exc:  # noqa: BLE001 — a broken guard must not block the farm
            log.warning("could not check for overlapping runs: %s", exc)
            return None
        for r in recent:
            started = r.get("startedAt")
            if r.get("status") == RUN_RUNNING and isinstance(started, datetime) and now - started < window:
                age = int((now - started).total_seconds() // 60)
                return f"a run started {age} min ago is still in progress"
        return None

    def _log_result(self, result: InvestigationResult) -> None:
        rep = self.report
        entry = build_log_entry(result, rep.run_id, rep.farm_name, self.repo.clock.now())
        log_id = self.repo.write_log(entry)
        rep.log_ids.append(log_id)
        self._upsert(decided=rep.decided, failed=rep.failed, decisions=_decision_counts(rep), lastLogId=log_id)
        verdict = result.decision.decision if result.decision else "FAILED"
        self.status("logged", f"{result.candidate.room_name}: {verdict}")

    def _fail(self, phase: str, exc: Exception) -> RunReport:
        rep = self.report
        rep.error = f"{type(exc).__name__}: {exc}"
        rep.status = RUN_FAILED
        rep.finished_at = self.repo.clock.now()
        log.exception("run %s failed during %s", rep.run_id, phase)
        try:
            log_id = self.repo.write_log(run_failure_entry(rep.run_id, rep.farm_name, phase, rep.error, rep.finished_at))
            rep.log_ids.append(log_id)
        except Exception:  # noqa: BLE001 — pragma: no cover
            log.exception("could not write the failure entry")
        self._upsert(status=RUN_FAILED, phase=PHASE_DONE, currentTarget=None, error=rep.error,
                     finishedAt=rep.finished_at, latencyMs=rep.latency_ms, decided=rep.decided, failed=rep.failed)
        self.status("failed", rep.error)
        return rep

    def _upsert(self, **fields: Any) -> None:
        fields.setdefault("startedAt", self.report.started_at)
        self.repo.upsert_run(self.report.run_id, **fields)


def _decision_counts(rep: RunReport) -> dict[str, int]:
    d = rep.decisions
    return {"NOISE": d.get("NOISE", 0), "WATCH": d.get("WATCH", 0), "ACT": d.get("ACT", 0)}


def run_once(repo: Any, cfg: Settings | None = None, **kwargs: Any) -> RunReport:
    """Convenience: one synchronous run."""
    return SentinelRun(repo, cfg, **kwargs).execute()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    from .firestore_client import describe_target, get_db
    from .repository import PigOpsRepository

    ap = argparse.ArgumentParser(prog="python -m sentinel.run", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--farm-id", default=default_settings.demo_farm_id)
    ap.add_argument("--trigger", default="manual", help="Recorded on the run doc (manual | scheduler | ...).")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if not args.verbose:
        import warnings
        warnings.filterwarnings("ignore", message=".*EXPERIMENTAL.*")
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        logging.getLogger("google_genai.types").setLevel(logging.ERROR)

    repo = PigOpsRepository(get_db(), args.farm_id)
    quiet = args.json

    def status(phase: str, detail: str) -> None:
        if not quiet:
            print(f"   … {phase}: {detail}")

    if not quiet:
        print(f"Target: {describe_target()} | model: {default_settings.gemini_model} @ {default_settings.google_cloud_location}")
    t0 = time.perf_counter()
    rep = run_once(repo, trigger=args.trigger, status=status)

    if args.json:
        print(json.dumps(rep.as_dict(), indent=2, ensure_ascii=False))
    else:
        print()
        print(f"Run {rep.run_id} — {rep.status.upper()}" + (f" ({rep.skip_reason})" if rep.skip_reason else ""))
        print(f"  {rep.farm_name or '(farm not loaded)'}: {len(rep.candidates)} candidate(s), {rep.decided} decided, {rep.failed} failed, "
              f"{dict(rep.decisions) or '{}'}, {rep.total_tokens} tokens, {time.perf_counter() - t0:.1f} s")
        for r, log_id in zip(rep.results, rep.log_ids):
            d = r.decision
            verdict = f"{d.decision}{' / ' + d.severity if d and d.severity else ''}" if d else f"FAILED ({r.error})"
            print(f"  [{r.candidate.signal_type}] {r.candidate.room_name}: {verdict}   → sentinelLog/{log_id}")
        if rep.error:
            print(f"  ERROR: {rep.error}")
    return 1 if rep.status == RUN_FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
