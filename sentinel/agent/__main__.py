"""Phase 1 → 2 → 3 end to end, without writing anything:

    python -m sentinel.agent            # scan the farm, investigate every candidate, print decisions
    python -m sentinel.agent --only MORTALITY_SPIKE
    python -m sentinel.agent --json

Useful while developing the agent; the real entry point (which also logs and
acts) is the run orchestrator added in later steps.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
import warnings

from ..config import settings
from ..firestore_client import describe_target, get_db
from ..repository import PigOpsRepository
from ..scanner import run_scan
from .investigator import investigate_candidates


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sentinel.agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--farm-id", default=settings.demo_farm_id)
    ap.add_argument("--only", help="Only investigate candidates of this signal type.")
    ap.add_argument("--limit", type=int, default=0, help="Investigate at most N candidates (0 = all).")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if not args.verbose:  # SDK chatter that is not actionable for a developer of this project
        warnings.filterwarnings("ignore", message=".*EXPERIMENTAL.*")
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        logging.getLogger("google_genai.types").setLevel(logging.ERROR)

    repo = PigOpsRepository(get_db(), args.farm_id)
    print(f"Target: {describe_target()} | model: {settings.gemini_model} @ {settings.google_cloud_location}")
    snapshot, candidates = run_scan(repo)
    if args.only:
        candidates = [c for c in candidates if c.signal_type == args.only]
    if args.limit:
        candidates = candidates[: args.limit]
    print(f"{snapshot.structure.farm.name}: {len(candidates)} candidate(s) to investigate\n")
    if not candidates:
        return 0

    run_id = f"dev-{uuid.uuid4().hex[:8]}"

    def status(phase: str, detail: str) -> None:
        if not args.json:
            print(f"   … {phase}: {detail}")

    results = investigate_candidates(repo, snapshot, candidates, run_id, status=status)

    if args.json:
        out = []
        for r in results:
            out.append({
                "signal": r.candidate.signal_type, "room": r.candidate.room_name, "observation": r.candidate.observation,
                "decision": r.decision.model_dump() if r.decision else None, "error": r.error,
                "tool_trace": r.trace_as_log(), "latency_ms": r.latency_ms, "tokens": r.total_tokens, "llm_calls": r.llm_calls,
            })
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    for r in results:
        c = r.candidate
        print("=" * 100)
        print(f"[{c.signal_type}] {c.room_name}")
        print(f"  Observed : {c.observation}")
        print(f"  Tools    : " + (", ".join(f"{t.tool}({', '.join(f'{k}={v!r}' for k, v in t.args.items())}){'' if t.ok else ' ✗'}" for t in r.tool_trace) or "(none)"))
        if r.decision:
            d = r.decision
            print(f"  Decision : {d.decision}" + (f" / {d.severity}" if d.severity else ""))
            print(f"  Reasoning: {d.reasoning}")
            if d.action_title:
                kind = "Notify   " if c.signal_type == "DEADLINE_RISK" else "Task     "
                print(f"  {kind}: {d.action_title}\n             {d.action_body}")
            if d.watch_reason:
                print(f"  Re-check : {d.watch_reason}")
        else:
            print(f"  FAILED   : {r.error}\n  raw: {r.raw_output[:400]}")
        print(f"  Cost     : {r.latency_ms} ms, {r.llm_calls} LLM call(s), {r.total_tokens} tokens ({r.prompt_tokens} in / {r.output_tokens} out)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
