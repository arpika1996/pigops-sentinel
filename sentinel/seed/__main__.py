"""Seed the demo farm into Firestore.

    python -m sentinel.seed                 # write the demo farm (idempotent: same IDs → overwrite)
    python -m sentinel.seed --reset         # wipe the demo farm first, then write
    python -m sentinel.seed --reset-log     # also wipe sentinelLog / sentinelRuns
    python -m sentinel.seed --dry-run       # build only, print the summary, write nothing
    python -m sentinel.seed --now 2026-08-15T09:30   # pin "now" (farm-local) for reproducible data

Targets whatever ``GOOGLE_CLOUD_PROJECT`` / ``FIRESTORE_EMULATOR_HOST`` point at
(see .env.example). Refuses to touch a project that does not look like a
sandbox unless ``--allow-any-project`` is given.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime

from google.cloud import firestore

from .. import schema as S
from ..clock import clock
from ..config import settings
from ..firestore_client import describe_target, get_db
from ..repository import batched
from .demo_data import BARNS, USERS, build_demo_plan

log = logging.getLogger("sentinel.seed")

SANDBOX_MARKERS = ("sentinel", "demo", "sandbox", "test", "dev")


def _looks_like_sandbox() -> bool:
    if settings.uses_emulator:
        return True
    pid = settings.google_cloud_project.lower()
    return any(m in pid for m in SANDBOX_MARKERS)


def reset_farm(db: firestore.Client, farm_id: str) -> None:
    log.info("Deleting telepek/%s (recursive)", farm_id)
    db.recursive_delete(db.document(S.farm_path(farm_id)))
    for barn_id, _ in BARNS:
        log.info("Deleting naplo/%s (recursive)", barn_id)
        db.recursive_delete(db.document(f"{S.COL_NAPLO}/{barn_id}"))
    for uid, *_ in USERS:
        db.document(f"{S.COL_USERS}/{uid}").delete()


def reset_log(db: firestore.Client) -> None:
    for col in (S.COL_SENTINEL_LOG, S.COL_SENTINEL_RUNS):
        log.info("Deleting collection %s (recursive)", col)
        db.recursive_delete(db.collection(col))


def write_plan(db: firestore.Client, plan) -> int:
    written = 0
    for chunk in batched(plan.writes, 400):
        batch = db.batch()
        for w in chunk:
            batch.set(db.document(w.path), w.data)
        batch.commit()
        written += len(chunk)
        log.info("  wrote %d / %d documents", written, len(plan.writes))
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sentinel.seed", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--farm-id", default=settings.demo_farm_id)
    ap.add_argument("--now", help="Pin 'now' as an ISO datetime (farm-local if no offset given).")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed (default 42).")
    ap.add_argument("--reset", action="store_true", help="Delete the demo farm (and its valve log) before writing.")
    ap.add_argument("--reset-log", action="store_true", help="Also delete sentinelLog and sentinelRuns.")
    ap.add_argument("--dry-run", action="store_true", help="Build the plan and print the summary; write nothing.")
    ap.add_argument("--allow-any-project", action="store_true", help="Skip the sandbox-project safety check.")
    ap.add_argument("--json", action="store_true", help="Print the summary as JSON.")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.now:
        clock.freeze(datetime.fromisoformat(args.now))

    plan = build_demo_plan(args.farm_id, clock, seed=args.seed)

    if args.dry_run:
        print(json.dumps(plan.summary, indent=2, ensure_ascii=False))
        return 0

    if not _looks_like_sandbox() and not args.allow_any_project:
        print(
            f"Refusing to seed into project {settings.google_cloud_project!r}: it does not look like a "
            "sandbox (name lacks sentinel/demo/sandbox/test/dev and no emulator is configured). "
            "Pass --allow-any-project if you really mean it.",
            file=sys.stderr,
        )
        return 2

    db = get_db()
    log.info("Target: %s", describe_target())
    if args.reset:
        reset_farm(db, args.farm_id)
    if args.reset_log:
        reset_log(db)
    n = write_plan(db, plan)
    log.info("Done: %d documents written for farm %r.", n, args.farm_id)
    if args.json:
        print(json.dumps(plan.summary, indent=2, ensure_ascii=False))
    else:
        print("\nPlanted anomalies (what the scanner should find today):")
        for sig, items in plan.summary["planted"].items():
            for it in items:
                print(f"  {sig:18s} {it}")
        print("Decoys (must NOT be found):")
        for d in plan.summary["decoys"]:
            print(f"  - {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
