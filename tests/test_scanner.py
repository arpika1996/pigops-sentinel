"""Phase 1 scanner: must find every planted anomaly, none of the decoys, and
must never leak a raw Firestore ID into human-readable text."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import timedelta

import pytest

from sentinel.config import Settings
from sentinel.scanner import Candidate, scan
from tests.fakes import snapshot_from_plan


@pytest.fixture(scope="module")
def cfg() -> Settings:
    # Explicit values so the tests do not depend on a developer's .env
    return Settings(
        mortality_spike_threshold=4,
        valve_change_threshold=25,
        valve_flap_threshold=5,
        silent_room_hours=36,
        deadline_soon_hours=24,
        _env_file=None,
    )


@pytest.fixture(scope="module")
def snapshot(demo_plan, frozen_clock):
    return snapshot_from_plan(demo_plan, frozen_clock)


@pytest.fixture(scope="module")
def candidates(snapshot, cfg) -> list[Candidate]:
    return scan(snapshot, cfg)


def _by_type(cands: list[Candidate], sig: str) -> dict[str, Candidate]:
    return {c.room_name: c for c in cands if c.signal_type == sig}


# ------------------------------------------------------------- planted set
def test_finds_exactly_the_planted_candidates(candidates):
    found = {(c.signal_type, c.room_name) for c in candidates}
    assert found == {
        ("MORTALITY_SPIKE", "B-Telep / 5.Terem"),
        ("MORTALITY_SPIKE", "A-Telep / 3.Terem"),
        ("VALVE_INSTABILITY", "A-Telep / 4.Terem"),
        ("SILENT_ROOM", "B-Telep / 6.Terem"),
        ("DEADLINE_RISK", "A-Telep / 2.Terem"),
        ("DEADLINE_RISK", "B-Telep / 3.Terem"),
    }


def test_decoys_are_not_candidates(candidates):
    names = {(c.signal_type, c.room_name) for c in candidates}
    assert ("MORTALITY_SPIKE", "A-Telep / 2.Terem") not in names      # 3 deaths < 4
    assert ("SILENT_ROOM", "B-Telep / 1.Terem") not in names          # empty room
    assert ("DEADLINE_RISK", "A-Telep / 5.Terem") not in names        # touched task
    titles = {c.task.title for c in candidates if c.task}
    assert "Takarmányadagoló beállítás – A-Telep / 5.Terem" not in titles
    assert "Havi fertőtlenítés – H2" not in titles


# ------------------------------------------------------------- per-rule
def test_mortality_spike_evidence(candidates):
    spikes = _by_type(candidates, "MORTALITY_SPIKE")
    clear = spikes["B-Telep / 5.Terem"]
    assert clear.evidence["deaths_today"] == 5 and clear.evidence["records_today"] == 2
    assert clear.evidence["causes_today"] == {"Légzőszervi": 5}
    assert "5 deaths recorded today in B-Telep / 5.Terem" in clear.observation
    chronic = spikes["A-Telep / 3.Terem"]
    assert chronic.evidence["deaths_today"] == 4  # exactly at threshold → still a candidate


def test_valve_instability_evidence(candidates):
    c = _by_type(candidates, "VALVE_INSTABILITY")["A-Telep / 4.Terem"]
    assert c.evidence["worst_valve"] == "A4-07"
    assert c.evidence["worst_valve_changes"] >= 8
    assert c.evidence["sign_flips"] >= 6
    assert "Valve A4-07 in A-Telep / 4.Terem" in c.observation


def test_silent_room_evidence(candidates):
    c = _by_type(candidates, "SILENT_ROOM")["B-Telep / 6.Terem"]
    assert c.evidence["pigs_in_room"] > 100
    assert c.evidence["hours_silent"] > 60
    assert "no data has been written" in c.observation


def test_deadline_risk_evidence(candidates):
    risks = {c.task.id: c for c in candidates if c.signal_type == "DEADLINE_RISK"}
    assert set(risks) == {"task-overdue", "task-due-soon"}
    assert risks["task-overdue"].evidence["kind"] == "overdue"
    assert risks["task-due-soon"].evidence["kind"] == "due_soon_untouched"
    assert 0 < risks["task-due-soon"].evidence["hours_left"] <= 24
    # people are named, not addressed by email
    assert "Halászi Réka" in risks["task-overdue"].observation
    assert "@" not in risks["task-overdue"].observation


# ------------------------------------------------------------- invariants
ID_PATTERNS = [r"\b[ab]-telep-t\d", r"-v\d+\b", r"demo-farm", r"\btask-[a-z]", r"demo-(admin|worker)"]


def test_no_raw_ids_in_human_text(candidates):
    for c in candidates:
        for pat in ID_PATTERNS:
            assert not re.search(pat, c.observation), (pat, c.observation)
            assert not re.search(pat, c.room_name), (pat, c.room_name)


def test_candidates_are_english_and_have_numbers(candidates):
    for c in candidates:
        assert re.search(r"\d", c.observation), c.observation
        assert c.observation.endswith("."), c.observation


def test_stable_ordering(snapshot, cfg):
    a = [c.key for c in scan(snapshot, cfg)]
    b = [c.key for c in scan(snapshot, cfg)]
    assert a == b


# ------------------------------------------------------------- thresholds
def test_thresholds_are_respected(snapshot, cfg):
    stricter = cfg.model_copy(update={"mortality_spike_threshold": 5})
    found = {(c.signal_type, c.room_name) for c in scan(snapshot, stricter)}
    assert ("MORTALITY_SPIKE", "A-Telep / 3.Terem") not in found
    assert ("MORTALITY_SPIKE", "B-Telep / 5.Terem") in found

    lenient_silence = cfg.model_copy(update={"silent_room_hours": 24 * 5})
    assert not any(c.signal_type == "SILENT_ROOM" for c in scan(snapshot, lenient_silence))

    lax_flap = cfg.model_copy(update={"valve_flap_threshold": 20, "valve_change_threshold": 100})
    assert not any(c.signal_type == "VALVE_INSTABILITY" for c in scan(snapshot, lax_flap))


def test_quiet_farm_yields_no_candidates(snapshot, cfg):
    """A normal day: nothing today, tasks far away, every room active → zero candidates, no LLM."""
    now = snapshot.now
    quiet_tasks = [replace(t, deadline=now + timedelta(days=7)) for t in snapshot.open_tasks]
    quiet_valves = {
        rid: [replace(v, last_modified=now - timedelta(hours=2)) for v in vs]
        for rid, vs in snapshot.valves.items()
    }
    quiet = replace(snapshot, mortality_today={}, valve_changes_today={}, open_tasks=quiet_tasks, valves=quiet_valves)
    assert scan(quiet, cfg) == []


def test_sentinel_created_tasks_are_left_to_follow_up(snapshot, cfg):
    """Overdue tasks the Sentinel itself created are not re-flagged as DEADLINE_RISK."""
    tagged = [replace(t, source="sentinel") for t in snapshot.open_tasks]
    snap = replace(snapshot, open_tasks=tagged)
    assert not any(c.signal_type == "DEADLINE_RISK" for c in scan(snap, cfg))
