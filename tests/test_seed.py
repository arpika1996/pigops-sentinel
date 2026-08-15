"""The demo dataset must be schema-faithful, deterministic and contain the
planted anomalies (and the decoys) exactly as documented."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone

from sentinel import schema as S
from sentinel.clock import Clock
from sentinel.seed.demo_data import (
    BARNS,
    FARM_ID_DEFAULT,
    ROOMS_PER_BARN,
    VALVES_PER_ROOM,
    build_demo_plan,
    room_id,
    valve_name,
)


def _by_prefix(plan, prefix: str):
    return [w for w in plan.writes if w.path.startswith(prefix)]


def _docs(plan, collection_segment: str):
    """All writes whose parent collection is `collection_segment`."""
    return [w for w in plan.writes if w.path.split("/")[-2] == collection_segment]


# ---------------------------------------------------------------- structure
def test_structure_counts(demo_plan):
    farm = [w for w in demo_plan.writes if w.path == f"{S.COL_TELEPEK}/{FARM_ID_DEFAULT}"]
    assert len(farm) == 1 and farm[0].data["nev"]  # farm name field is `nev`
    barns = _docs(demo_plan, S.COL_HIZLALDAK)
    assert {w.data["name"] for w in barns} == {"H1", "H2"}  # barn name field is `name`
    rooms = _docs(demo_plan, S.COL_TERMEK)
    assert len(rooms) == len(BARNS) * ROOMS_PER_BARN
    valves = _docs(demo_plan, S.COL_SZELEPEK)
    assert len(valves) == len(BARNS) * ROOMS_PER_BARN * VALVES_PER_ROOM


def test_valve_numbering_encodes_the_room(demo_plan):
    """Room 1 → 101–116, room 2 → 201–216 … (farm convention)."""
    for w in _docs(demo_plan, S.COL_SZELEPEK):
        segs = w.path.split("/")
        terem = segs[segs.index(S.COL_TERMEK) + 1]      # e.g. h1-t4
        room_idx = int(terem.rsplit("t", 1)[1])
        n = int(w.data["name"])
        assert n // 100 == room_idx, w.path
        assert 1 <= n % 100 <= VALVES_PER_ROOM
    assert valve_name(4, 7) == "407"


def test_every_valve_and_change_carries_telep_id(demo_plan):
    for w in _docs(demo_plan, S.COL_SZELEPEK):
        assert w.data["telepId"] == FARM_ID_DEFAULT
        assert isinstance(w.data["szelep"], int)
    changes = _docs(demo_plan, S.COL_MODOSITASOK)
    assert changes, "valve log must not be empty"
    for w in changes:
        assert w.path.startswith(f"{S.COL_NAPLO}/"), "valve log lives in the separate naplo/ tree"
        for k in ("telepId", "hizlaldaId", "teremId", "szelepId", "szelepName", "modositas", "datum"):
            assert k in w.data, (k, w.path)
        assert isinstance(w.data["datum"], datetime) and w.data["datum"].tzinfo is not None


def test_mortality_records_are_app_shaped(demo_plan):
    for w in _docs(demo_plan, S.COL_ELHULLASOK):
        for k in ("telepId", "hizlaldaId", "hizlaldaName", "teremId", "teremName", "szelepId", "szelepName", "darab", "ok", "datum", "createdAt"):
            assert k in w.data, (k, w.path)
        assert w.data["darab"] >= 1


def test_tasks_are_app_shaped(demo_plan):
    tasks = _docs(demo_plan, S.COL_FELADATOK)
    assert len(tasks) == 7
    for w in tasks:
        d = w.data
        assert isinstance(d["done"], bool)
        assert d["status"] in (S.TASK_STATUS_OPEN, S.TASK_STATUS_DONE)
        assert "assigneeEmail" in d and "createdAt" in d and "hizlaldaId" in d and "hizlaldaNev" in d
        if d["done"]:
            assert "completedAt" in d


def test_people_and_roles(demo_plan):
    perms = _docs(demo_plan, S.COL_JOGOSULTSAGOK)
    roles = Counter(w.data["role"] for w in perms)
    assert roles[S.ROLE_ADMIN] == 1 and roles[S.ROLE_USER] == 2
    for w in perms:
        assert w.path.endswith("/" + w.data["uid"]), "jogosultsagok doc ID must equal the uid"
    users = _docs(demo_plan, S.COL_USERS)
    assert len(users) == 3
    for w in users:
        assert FARM_ID_DEFAULT in w.data["telepIds"]
        assert "fcmToken" not in w.data


# ------------------------------------------------------------ planted facts
def _deaths_today_by_room(plan, clock: Clock):
    out = defaultdict(int)
    for w in _docs(plan, S.COL_ELHULLASOK):
        if clock.local_day(w.data["datum"]) == clock.today():
            out[w.data["teremId"]] += w.data["darab"]
    return out


def test_planted_mortality_spike_and_decoy(demo_plan, frozen_clock):
    deaths = _deaths_today_by_room(demo_plan, frozen_clock)
    assert deaths[room_id("h2", 5)] == 5     # clear spike
    assert deaths[room_id("h1", 3)] == 4     # chronic room, exactly at threshold
    assert deaths[room_id("h1", 2)] == 3     # decoy: below threshold


def test_planted_valve_flapping(demo_plan, frozen_clock):
    per_valve = Counter()
    for w in _docs(demo_plan, S.COL_MODOSITASOK):
        if w.data["teremId"] == room_id("h1", 4) and frozen_clock.local_day(w.data["datum"]) == frozen_clock.today():
            per_valve[w.data["szelepName"]] += 1
    assert per_valve["407"] >= 8
    # the valve doc's roll-up fields agree with the log
    v407 = next(w for w in _docs(demo_plan, S.COL_SZELEPEK) if w.data["name"] == "407" and "/h1-t4/" in w.path)
    assert v407.data["modositas_szama"] == per_valve["407"]
    assert v407.data["last_modified_day"] == frozen_clock.today().isoformat()


def test_planted_silent_room_and_empty_decoy(demo_plan, frozen_clock):
    last = defaultdict(lambda: None)
    pigs = Counter()
    for w in _docs(demo_plan, S.COL_SZELEPEK):
        segs = w.path.split("/")
        terem = segs[segs.index(S.COL_TERMEK) + 1]
        pigs[terem] += w.data["szelep"]
        lm = w.data.get("last_modified")
        if lm and (last[terem] is None or lm > last[terem]):
            last[terem] = lm
    silent = room_id("h2", 6)
    empty = room_id("h2", 1)
    assert pigs[silent] > 100 and frozen_clock.hours_since(last[silent]) > 60
    assert pigs[empty] == 0 and frozen_clock.hours_since(last[empty]) > 24 * 10
    # every other populated room was active within the last day
    for terem, n in pigs.items():
        if terem not in (silent, empty):
            assert frozen_clock.hours_since(last[terem]) < 24, terem


def test_planted_deadline_risks(demo_plan, frozen_clock):
    tasks = {w.path.split("/")[-1]: w.data for w in _docs(demo_plan, S.COL_FELADATOK)}
    now = frozen_clock.now()
    assert tasks["task-overdue"]["deadline"] < now and not tasks["task-overdue"]["done"]
    soon = tasks["task-due-soon"]["deadline"]
    assert 0 < (soon - now).total_seconds() / 3600 <= 24
    worklog = Counter(w.path.split("/")[-3] for w in _docs(demo_plan, S.COL_MUNKAK))
    assert worklog["task-overdue"] == 0 and worklog["task-due-soon"] == 0
    assert worklog["task-in-progress"] >= 1      # decoy: touched


def test_deterministic_for_same_now(frozen_clock):
    a = build_demo_plan(FARM_ID_DEFAULT, frozen_clock, seed=42)
    b = build_demo_plan(FARM_ID_DEFAULT, frozen_clock, seed=42)
    assert [(w.path, w.data) for w in a.writes] == [(w.path, w.data) for w in b.writes]


def test_nothing_is_dated_in_the_future(demo_plan, frozen_clock):
    now = frozen_clock.now()
    for w in demo_plan.writes:
        for k in ("datum", "createdAt", "last_modified"):
            v = w.data.get(k)
            if isinstance(v, datetime):
                assert v <= now, (w.path, k)


def test_all_timestamps_are_tz_aware(demo_plan):
    for w in demo_plan.writes:
        for k, v in w.data.items():
            if isinstance(v, datetime):
                assert v.tzinfo is not None, (w.path, k)
                assert v.utcoffset() == timezone.utc.utcoffset(None) or v.tzinfo is not None
