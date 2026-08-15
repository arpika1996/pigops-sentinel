"""The decision log — one ``sentinelLog`` document per investigated candidate.

This is the product (SPEC §5): the console renders it, and it is what proves
the agent decided something on its own — including deciding that a room is
*fine* (NOISE is logged like everything else) and including the cases where
the investigation itself failed ("fail loudly, not silently", SPEC §8).

Field names follow SPEC §5 exactly; a few extras are added for the console
and for the later phases (marked below). Rule kept everywhere: names, never
raw IDs — the only ID the log may carry is ``actionTaken.taskId`` (SPEC §5).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .agent.investigator import InvestigationResult
from .agent.schemas import Decision
from .scanner import Candidate

STATUS_DECIDED = "decided"     # the model returned a valid decision
STATUS_FAILED = "failed"       # investigation error / no valid decision (still logged)
STATUS_RUN_FAILED = "run_failed"  # the run itself broke before/while investigating

SIGNAL_RUN = "RUN"             # signalType of run-level entries (no room)

# What ACT means per signal type: DEADLINE_RISK already has its task, so ACT
# is a notification about it; every other signal creates a task (SPEC §4/§5).
NOTIFY_SIGNALS = frozenset({"DEADLINE_RISK"})


def action_kind(signal_type: str) -> str:
    """'notification' or 'task' — the action an ACT decision turns into."""
    return "notification" if signal_type in NOTIFY_SIGNALS else "task"


def build_log_entry(result: InvestigationResult, run_id: str, farm_name: str, at: datetime) -> dict[str, Any]:
    """The sentinelLog document for one investigated candidate (decided or failed)."""
    c = result.candidate
    d = result.decision
    entry: dict[str, Any] = {
        # ---- SPEC §5 ------------------------------------------------------
        "runId": run_id,
        "timestamp": at,
        "farmName": farm_name,
        "roomName": c.room_name,
        "signalType": c.signal_type,
        "observation": c.observation,
        "contextGathered": context_gathered(result),
        "reasoning": d.reasoning if d else "",
        "decision": d.decision if d else None,
        "severity": d.severity if d else None,
        "actionTaken": None,      # filled by Phase 4 (action tools)
        "escalation": None,       # filled by Phase 5 (follow-up)
        "latencyMs": result.latency_ms,
        # ---- extras (console + later phases) ------------------------------
        "status": STATUS_DECIDED if result.ok else STATUS_FAILED,
        "error": result.error,
        "model": result.model,
        "llmCalls": result.llm_calls,
        "tokens": {"prompt": result.prompt_tokens, "output": result.output_tokens, "total": result.total_tokens},
        "toolCalls": len(result.tool_trace),
        "toolFailures": sum(1 for t in result.tool_trace if not t.ok),
        "proposedAction": proposed_action(c, d),
        "watchReason": d.watch_reason if d and d.decision == "WATCH" else None,
    }
    if not result.ok:
        # keep what the model actually said so a bad day can be debugged from the console
        entry["rawOutput"] = (result.raw_output or "")[:2000]
    return entry


def run_failure_entry(run_id: str, farm_name: str, phase: str, error: str, at: datetime) -> dict[str, Any]:
    """A run that broke before it could decide anything still leaves a trace."""
    return {
        "runId": run_id,
        "timestamp": at,
        "farmName": farm_name,
        "roomName": "",
        "signalType": SIGNAL_RUN,
        "observation": f"The Sentinel run failed during the {phase} phase.",
        "contextGathered": [],
        "reasoning": "",
        "decision": None,
        "severity": None,
        "actionTaken": None,
        "escalation": None,
        "latencyMs": 0,
        "status": STATUS_RUN_FAILED,
        "error": error,
        "phase": phase,
    }


def proposed_action(c: Candidate, d: Decision | None) -> dict[str, Any] | None:
    """ACT only: what Phase 4 is asked to do. Recorded before it is done, so a
    failed action is still visible next to the decision that wanted it."""
    if d is None or d.decision != "ACT":
        return None
    return {"kind": action_kind(c.signal_type), "title": d.action_title, "body": d.action_body}


def context_gathered(result: InvestigationResult) -> list[dict[str, Any]]:
    """Which tools were called and why (SPEC §5).

    The *tool trace* is authoritative — it is what actually ran. The model's
    own ``context_gathered`` claims are merged onto it (``why`` / ``finding``)
    by tool name, in order. A claim with no matching call is kept but flagged
    ``verified: false``: the reader sees the model saying it looked when it
    did not, instead of the discrepancy being hidden.
    """
    claims = list(result.decision.context_gathered) if result.decision else []
    items: list[dict[str, Any]] = []
    for call in result.tool_trace:
        item = call.as_log()
        for i, claim in enumerate(claims):
            if claim.tool == call.tool:
                item["why"] = claim.why
                item["finding"] = claim.finding
                claims.pop(i)
                break
        items.append(item)
    for claim in claims:
        items.append({
            "tool": claim.tool,
            "args": {},
            "ok": False,
            "verified": False,
            "durationMs": 0,
            "summary": "claimed by the model but not present in the tool trace",
            "why": claim.why,
            "finding": claim.finding,
        })
    return items
