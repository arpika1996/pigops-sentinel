"""The agent's read tools: names in, names out; every call traced; errors
surfaced to the model instead of raised. No LLM, no Firestore."""

from __future__ import annotations

import json
import re

import pytest

from sentinel.agent.tools import ToolBox
from sentinel.config import Settings
from tests.fakes import FakeRepository, snapshot_from_plan

ID_PATTERNS = [r"\b[ab]-telep-t\d", r"-v\d+\b", r"demo-farm", r"\btask-[a-z]", r"demo-(admin|worker)"]


@pytest.fixture()
def box(demo_plan, frozen_clock):
    snap = snapshot_from_plan(demo_plan, frozen_clock)
    repo = FakeRepository(demo_plan, frozen_clock)
    return ToolBox(repo, snap, Settings(_env_file=None))


@pytest.fixture()
def fns(box):
    """{tool name: callable} as the LlmAgent would receive them."""
    return {t.name: t.func for t in box.tools()}


def _assert_no_ids(obj) -> None:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    for pat in ID_PATTERNS:
        assert not re.search(pat, text), (pat, text[:300])


def test_tool_names_and_docs(fns):
    assert set(fns) == {"get_structure", "get_room_stats", "get_mortality_history", "get_farm_mortality_overview", "get_open_tasks", "get_valve_log"}
    for name, fn in fns.items():
        assert fn.__doc__ and len(fn.__doc__) > 40, name


def test_structure_is_names_only(fns, box):
    out = fns["get_structure"]()
    assert out["farm"] == "PigOps"
    assert [b["barn"] for b in out["barns"]] == ["A-Telep", "B-Telep"]
    rooms = [r["room"] for b in out["barns"] for r in b["rooms"]]
    assert "B-Telep / 5.Terem" in rooms and len(rooms) == 12
    _assert_no_ids(out)
    assert box.trace[-1].tool == "get_structure" and box.trace[-1].ok


def test_room_stats(fns):
    out = fns["get_room_stats"](room="B-Telep / 5.Terem")
    assert out["room"] == "B-Telep / 5.Terem"
    assert out["deaths_today"] == 5 and out["death_causes_today"] == {"Légzőszervi": 2}  # 2 records
    assert out["pigs"] > 100 and out["valves"] == 16
    _assert_no_ids(out)


def test_room_name_is_tolerant(fns):
    a = fns["get_room_stats"](room="B-Telep / 5.Terem")
    b = fns["get_room_stats"](room="b-telep/5.terem")
    c = fns["get_room_stats"](room="B-Telep 5. Terem")
    assert a["room"] == b["room"] == c["room"] == "B-Telep / 5.Terem"


def test_unknown_room_is_an_error_not_an_exception(fns, box):
    out = fns["get_room_stats"](room="H9 / 1. terem")
    assert "error" in out and "Unknown room" in out["error"] and "A-Telep / 1.Terem" in out["error"]
    assert box.trace[-1].ok is False and box.trace[-1].error


def test_mortality_history_uses_the_repository(fns, box):
    out = fns["get_mortality_history"](room="B-Telep / 5.Terem", days=14)
    assert out["days"] == 14 and len(out["series"]) == 14
    assert out["today"] == 5
    assert out["previous_days_mean"] is not None and out["previous_days_mean"] < 1.5
    assert ("get_mortality_history", "b-telep-t5", 14) in box.repo.calls
    _assert_no_ids(out)


def test_farm_overview_marks_the_spike_room(fns):
    out = fns["get_farm_mortality_overview"](days=7)
    rows = {r["room"]: r for r in out["rooms"]}
    assert rows["B-Telep / 5.Terem"]["deaths_today"] == 5
    assert "B-Telep / 1.Terem" not in rows or rows["B-Telep / 1.Terem"]["pigs"] == 0
    _assert_no_ids(out)


def test_open_tasks_room_and_farm(fns):
    room = fns["get_open_tasks"](room="A-Telep / 3.Terem")
    assert room["count"] == 1 and room["tasks"][0]["work_log_entries"] == 4
    assert room["tasks"][0]["assignee"] == "Csontos Bertalan"
    farm = fns["get_open_tasks"]()
    assert farm["room"] == "whole farm" and farm["count"] == 5
    _assert_no_ids(farm)


def test_valve_log_shows_the_flapping_valve(fns):
    out = fns["get_valve_log"](room="A-Telep / 4.Terem", days=1)
    worst = out["per_valve"][0]
    assert worst["valve"] == "A4-07" and worst["changes"] >= 8 and worst["sign_flips"] >= 6
    assert out["recent_entries"] and "valve" in out["recent_entries"][0]
    _assert_no_ids(out)


def test_trace_records_every_call_in_order(fns, box):
    box.trace.clear()
    fns["get_structure"]()
    fns["get_room_stats"](room="A-Telep / 1.Terem")
    fns["get_valve_log"](room="A-Telep / 1.Terem", days=2)
    assert [t.tool for t in box.trace] == ["get_structure", "get_room_stats", "get_valve_log"]
    assert box.trace[2].args == {"room": "A-Telep / 1.Terem", "days": 2}
    for t in box.trace:
        assert t.ok and t.summary and t.duration_ms >= 0
