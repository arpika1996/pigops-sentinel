"""sentinelLog entries: SPEC §5 shape, names only, the trace is authoritative,
failures are logged too. Pure — no LLM, no Firestore."""

from __future__ import annotations

from datetime import datetime

import pytest

from sentinel.agent.investigator import InvestigationResult
from sentinel.agent.schemas import ContextItem, Decision
from sentinel.agent.tools import ToolCall
from sentinel.config import Settings
from sentinel.logbook import (
    STATUS_DECIDED, STATUS_FAILED, STATUS_RUN_FAILED, SIGNAL_RUN,
    action_kind, build_log_entry, context_gathered, run_failure_entry,
)
from sentinel.scanner import scan
from tests.fakes import snapshot_from_plan

AT = datetime(2026, 8, 15, 10, 31)
SPEC_FIELDS = {"runId", "timestamp", "farmName", "roomName", "signalType", "observation", "contextGathered",
               "reasoning", "decision", "severity", "actionTaken", "escalation", "latencyMs"}


@pytest.fixture(scope="module")
def candidates(demo_plan, frozen_clock):
    snap = snapshot_from_plan(demo_plan, frozen_clock)
    return {c.signal_type: c for c in scan(snap, Settings(_env_file=None))}  # one per type is enough here


def _call(tool: str, ok: bool = True, **args) -> ToolCall:
    return ToolCall(tool=tool, args=args, ok=ok, duration_ms=12, summary=f"{tool} ok", error=None if ok else "boom")


def _result(candidate, decision: Decision | None, trace=(), error=None, raw="") -> InvestigationResult:
    return InvestigationResult(candidate=candidate, decision=decision, raw_output=raw, tool_trace=list(trace),
                               latency_ms=1234, prompt_tokens=100, output_tokens=20, total_tokens=120, llm_calls=3,
                               model="gemini-test", error=error)


def test_action_kind():
    assert action_kind("DEADLINE_RISK") == "notification"
    for s in ("MORTALITY_SPIKE", "SILENT_ROOM", "VALVE_INSTABILITY"):
        assert action_kind(s) == "task"


def test_decided_entry_has_spec_fields_and_extras(candidates):
    c = candidates["MORTALITY_SPIKE"]
    d = Decision(decision="ACT", severity="high", reasoning="5 today against a 14-day mean of 0.7.",
                 action_title="Check the room", action_body="Call the vet.",
                 context_gathered=[ContextItem(tool="get_mortality_history", why="baseline", finding="mean 0.7")])
    e = build_log_entry(_result(c, d, [_call("get_room_stats", room=c.room_name), _call("get_mortality_history", room=c.room_name, days=14)]),
                        "run-1", "PigOps Sertéstelep (demo)", AT)
    assert SPEC_FIELDS <= set(e)
    assert e["runId"] == "run-1" and e["timestamp"] == AT and e["farmName"].startswith("PigOps")
    assert e["roomName"] == c.room_name and e["signalType"] == "MORTALITY_SPIKE" and e["observation"] == c.observation
    assert e["decision"] == "ACT" and e["severity"] == "high" and e["reasoning"].startswith("5 today")
    assert e["actionTaken"] is None and e["escalation"] is None       # Phase 4/5 fill these
    assert e["status"] == STATUS_DECIDED and e["error"] is None
    assert e["latencyMs"] == 1234 and e["llmCalls"] == 3 and e["tokens"] == {"prompt": 100, "output": 20, "total": 120}
    assert e["toolCalls"] == 2 and e["toolFailures"] == 0
    assert e["proposedAction"] == {"kind": "task", "title": "Check the room", "body": "Call the vet."}
    assert e["watchReason"] is None and "rawOutput" not in e


def test_deadline_act_proposes_a_notification_not_a_task(candidates):
    c = candidates["DEADLINE_RISK"]
    d = Decision(decision="ACT", severity="medium", reasoning="Overdue and untouched.", action_title="Reminder", action_body="Do it today.")
    e = build_log_entry(_result(c, d), "run-1", "farm", AT)
    assert e["proposedAction"]["kind"] == "notification"


def test_watch_and_noise_entries(candidates):
    c = candidates["VALVE_INSTABILITY"]
    w = Decision(decision="WATCH", reasoning="Back-and-forth edits, net zero.", watch_reason="Another 5 edits tomorrow.")
    e = build_log_entry(_result(c, w), "run-1", "farm", AT)
    assert e["decision"] == "WATCH" and e["severity"] is None
    assert e["proposedAction"] is None and e["watchReason"] == "Another 5 edits tomorrow."

    n = Decision(decision="NOISE", reasoning="Within the treated outbreak's range.")
    e = build_log_entry(_result(candidates["MORTALITY_SPIKE"], n), "run-1", "farm", AT)
    assert e["decision"] == "NOISE" and e["proposedAction"] is None and e["watchReason"] is None
    assert e["status"] == STATUS_DECIDED   # NOISE is a decision, logged like any other


def test_failed_investigation_is_still_an_entry(candidates):
    c = candidates["SILENT_ROOM"]
    e = build_log_entry(_result(c, None, [_call("get_room_stats", ok=False, room=c.room_name)], error="TimeoutError: model", raw="{partial"),
                        "run-1", "farm", AT)
    assert e["status"] == STATUS_FAILED and e["error"] == "TimeoutError: model"
    assert e["decision"] is None and e["severity"] is None and e["reasoning"] == ""
    assert e["toolFailures"] == 1 and e["rawOutput"] == "{partial"
    assert e["roomName"] == c.room_name and e["observation"] == c.observation   # the observation survives


def test_context_merges_claims_onto_the_real_trace(candidates):
    c = candidates["MORTALITY_SPIKE"]
    d = Decision(decision="NOISE", reasoning="fine", context_gathered=[
        ContextItem(tool="get_mortality_history", why="need the baseline", finding="mean 0.7"),
        ContextItem(tool="get_valve_log", why="ruled out water", finding="no changes"),      # never actually called
    ])
    r = _result(c, d, [_call("get_room_stats", room=c.room_name), _call("get_mortality_history", room=c.room_name, days=14)])
    items = context_gathered(r)
    assert [i["tool"] for i in items] == ["get_room_stats", "get_mortality_history", "get_valve_log"]
    assert "why" not in items[0]                                   # real call, no claim about it
    assert items[1]["why"] == "need the baseline" and items[1]["finding"] == "mean 0.7" and items[1]["ok"] is True
    assert items[2]["verified"] is False and items[2]["ok"] is False   # claimed, not done — visible, not hidden
    assert items[1]["args"] == {"room": c.room_name, "days": 14}


def test_run_failure_entry():
    e = run_failure_entry("run-9", "farm", "scanning", "RuntimeError: Firestore down", AT)
    assert SPEC_FIELDS <= set(e)
    assert e["signalType"] == SIGNAL_RUN and e["status"] == STATUS_RUN_FAILED and e["phase"] == "scanning"
    assert e["error"] == "RuntimeError: Firestore down" and e["decision"] is None and e["roomName"] == ""
    assert "scanning" in e["observation"]
