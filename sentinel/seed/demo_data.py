"""Builds the demo farm as a list of Firestore writes — pure, deterministic.

The dataset mirrors the production PigOps schema field-for-field (see
:mod:`sentinel.schema`) and is generated relative to a given ``now`` so the
planted anomalies are always "today". A fixed RNG seed makes two runs with the
same ``now`` produce identical data.

Farm layout
-----------
* ``PigOps`` — the PigOps *demo* farm (the one that exists in the app as a
  playground: barns ``A-Telep`` and ``B-Telep``, rooms ``1.Terem`` …),
  extended to 6 rooms per barn and 16 valves per room. Valve names encode
  barn and room: ``A5-04`` = valve 4 of A-Telep / 5.Terem. Deliberately
  unlike any real PigOps farm (those are H1–H8 / M1–M2 with 101–116 valves).
* People are fictional (Fenyvesi Márton — farm admin; Halászi Réka and
  Csontos Bertalan — workers), e-mails on the reserved ``.example`` domain.
* 21 days of history: mortality records, valve modifications, counting days.

Planted anomalies (what the Phase-1 scanner must find today)
------------------------------------------------------------
1. ``MORTALITY_SPIKE``   B-Telep / 5.Terem — 5 deaths today (2 records) against
                         a 14-day mean well below 1/day. Clear ACT.
2. ``MORTALITY_SPIKE``   A-Telep / 3.Terem — 4 deaths today, BUT the room has a
                         known respiratory problem (≈2.5/day for 10 days) and
                         an open treatment task with daily work-log entries.
                         A candidate the agent should judge NOISE/WATCH.
3. ``VALVE_INSTABILITY`` A-Telep / 4.Terem — valve A4-07 changed 8× today,
                         flipping +2/−2 (data-entry flapping).
4. ``SILENT_ROOM``       B-Telep / 6.Terem — populated (≈190 pigs) but no data
                         written for ~3 days.
5. ``DEADLINE_RISK``     task "Szellőző ventilátor javítása – A-Telep / 2.Terem"
                         overdue by 2 days, never touched.
6. ``DEADLINE_RISK``     task "Itatók ellenőrzése – B-Telep / 3.Terem" due in
                         6 h, never touched.

Decoys (must NOT be candidates)
-------------------------------
* A-Telep / 2.Terem — 3 deaths today: below the absolute threshold of 4.
* B-Telep / 1.Terem — empty room between batches, silent for 12 days: silence
  only matters for populated rooms.
* task "Takarmányadagoló beállítás – A-Telep / 5.Terem" — due in 10 h but has
  work-log entries, i.e. someone is on it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .. import schema as S
from ..clock import Clock

# --------------------------------------------------------------------------
# Output shape
# --------------------------------------------------------------------------
@dataclass
class Write:
    path: str  # full document path
    data: dict[str, Any]


@dataclass
class SeedPlan:
    writes: list[Write] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def add(self, path: str, data: dict[str, Any]) -> None:
        self.writes.append(Write(path, data))


# --------------------------------------------------------------------------
# Static demo entities
# --------------------------------------------------------------------------
# Everything a viewer can read is defined here, in one place. It mirrors the
# "PigOps" demo farm that already exists in the app (A-Telep / B-Telep,
# "1.Terem" spelling) and stays clearly apart from the real farms.
FARM_ID_DEFAULT = "demo-farm"
FARM_NAME = "PigOps"

BARNS = [("a-telep", "A-Telep"), ("b-telep", "B-Telep")]
ROOMS_PER_BARN = 6
VALVES_PER_ROOM = 16

USERS = [
    # uid, display name, email, farm role — fictional people, reserved domain
    ("demo-admin", "Fenyvesi Márton", "marton.fenyvesi@pigops-demo.example", S.ROLE_ADMIN),
    ("demo-worker-1", "Halászi Réka", "reka.halaszi@pigops-demo.example", S.ROLE_USER),
    ("demo-worker-2", "Csontos Bertalan", "bertalan.csontos@pigops-demo.example", S.ROLE_USER),
]

MORTALITY_CAUSES = ["Légzőszervi", "Hasmenés", "Sérülés", "Hirtelen szívhalál", "Ismeretlen", "Farokrágás"]

# Special rooms (barn id, room index 1..6)
SPIKE_ROOM = ("b-telep", 5)          # clear mortality spike
CHRONIC_ROOM = ("a-telep", 3)        # known problem, open treatment task
DECOY_MORTALITY_ROOM = ("a-telep", 2)  # 3 today — below threshold
FLAP_ROOM = ("a-telep", 4)           # valve flapping
FLAP_VALVE_NO = 7                    # valve "A4-07"
SILENT_ROOM = ("b-telep", 6)         # populated, no data ~3 days
EMPTY_ROOM = ("b-telep", 1)          # between batches, legitimately silent

HISTORY_DAYS = 21


def room_id(barn_id: str, idx: int) -> str:
    return f"{barn_id}-t{idx}"


def room_name(idx: int) -> str:
    """The app's own spelling: "1.Terem" (no space, capital T)."""
    return f"{idx}.Terem"


def room_label(barn_id: str, idx: int) -> str:
    """What a human sees: "A-Telep / 5.Terem"."""
    return f"{dict(BARNS)[barn_id]} / {room_name(idx)}"


def valve_id(barn_id: str, idx: int, n: int) -> str:
    return f"{barn_id}-t{idx}-v{n}"


def valve_name(barn_id: str, room_idx: int, n: int) -> str:
    """Barn letter + room + valve: A-Telep / 5.Terem valve 4 → "A5-04"."""
    return f"{dict(BARNS)[barn_id][0]}{room_idx}-{n:02d}"


# --------------------------------------------------------------------------
# Builder
# --------------------------------------------------------------------------
class DemoDataBuilder:
    def __init__(self, farm_id: str, clock: Clock, seed: int = 42) -> None:
        self.farm_id = farm_id
        self.clock = clock
        self.rng = random.Random(seed)
        self.now = clock.now()
        self.today = clock.today()
        self.today_start, _ = clock.day_bounds(self.today)
        self.plan = SeedPlan()
        # running state
        self.valve_counts: dict[str, int] = {}          # valve id -> pigs
        self.valve_last: dict[str, datetime] = {}       # valve id -> last modification
        self.valve_today: dict[str, list[int]] = {}     # valve id -> today's deltas
        self.room_last_activity: dict[str, datetime] = {}
        self.stats = {"elhullasok": 0, "modositasok": 0, "feladatok": 0, "szelepek": 0}

    # ---------------------------------------------------------------- utils
    def _farm(self, *segments: str) -> str:
        return "/".join([S.COL_TELEPEK, self.farm_id, *segments])

    def _t_local(self, day_offset: int, hour: float) -> datetime:
        """An instant on (today - day_offset) at farm-local ``hour`` (fractional)."""
        day = self.today - timedelta(days=day_offset)
        start, _ = self.clock.day_bounds(day)
        return start + timedelta(hours=hour)

    def _t_today_before_now(self, fraction: float) -> datetime:
        """A point today at ``fraction`` of the working time elapsed so far — always <= now.

        The working day starts at 05:30 farm-local; if it is still early, the
        window falls back to "since midnight" so seeded events never sit in the
        future.
        """
        work_start = self.today_start + timedelta(hours=5, minutes=30)
        start = work_start if self.now > work_start + timedelta(hours=1) else self.today_start
        elapsed = self.now - start
        return start + elapsed * max(0.02, min(fraction, 0.98))

    def _working_hour(self) -> float:
        return self.rng.uniform(5.5, 17.0)

    # ------------------------------------------------------------ structure
    def build(self) -> SeedPlan:
        self._farm_and_people()
        self._structure_and_valves()
        self._history()
        self._today()
        self._tasks()
        self._finalise_valves()
        self._summary()
        return self.plan

    def _farm_and_people(self) -> None:
        created = self.now - timedelta(days=120)
        self.plan.add(self._farm(), {
            "nev": FARM_NAME,
            "telepTipus": "hizlalda",
            "createdAt": created,
            "createdBy": USERS[0][0],
        })
        for uid, name, email, role in USERS:
            self.plan.add(f"{S.COL_USERS}/{uid}", {
                "uid": uid,
                "email": email,
                "displayName": name,
                "role": S.ROLE_USER,          # global role; the farm role lives in jogosultsagok
                "telepId": self.farm_id,
                "telepIds": [self.farm_id],
                "notifyEnabled": True,
                "notifyTime": "06:00",
                "createdAt": created,
                "updatedAt": created,
                # no fcmToken on purpose — the demo has no real devices
            })
            self.plan.add(self._farm(S.COL_JOGOSULTSAGOK, uid), {
                "uid": uid,
                "role": role,
                "createdAt": created + timedelta(minutes=USERS.index((uid, name, email, role))),
                "updatedAt": created,
                "createdByUid": USERS[0][0],
                "aktivitasErtesites": role == S.ROLE_ADMIN,
            })
        for i, cause in enumerate(MORTALITY_CAUSES):
            self.plan.add(self._farm(S.COL_ELHULLAS_OKOK, f"ok-{i+1}"), {"nev": cause})

    def _structure_and_valves(self) -> None:
        for barn_id, barn_name in BARNS:
            self.plan.add(self._farm(S.COL_HIZLALDAK, barn_id), {"name": barn_name})
            for idx in range(1, ROOMS_PER_BARN + 1):
                rid = room_id(barn_id, idx)
                empty = (barn_id, idx) == EMPTY_ROOM
                nap = 0 if empty else self.rng.randint(18, 96)
                self.plan.add(self._farm(S.COL_HIZLALDAK, barn_id, S.COL_TERMEK, rid), {
                    "name": room_name(idx),
                    "order": idx,
                    "hizlalasiNap": nap,
                    "becsultSuly": 0.0 if empty else round(28 + nap * 0.78, 1),
                    "teremProblems": [],
                    "problemCount": 0,
                    "szelepHibaCount": 0,
                    "createdAt": self.now - timedelta(days=120),
                    "updatedAt": self.now - timedelta(days=1),
                })
                for n in range(1, VALVES_PER_ROOM + 1):
                    vid = valve_id(barn_id, idx, n)
                    self.valve_counts[vid] = 0 if empty else self.rng.randint(11, 13)
                    self.valve_today[vid] = []
                # empty room: last touched 12 days ago (cleaning after the batch left)
                if empty:
                    t = self._t_local(12, 10.0)
                    self.room_last_activity[rid] = t
                    for n in range(1, VALVES_PER_ROOM + 1):
                        self.valve_last[valve_id(barn_id, idx, n)] = t

    # -------------------------------------------------------------- history
    def _populated_rooms(self) -> list[tuple[str, int]]:
        return [(b, i) for b, _ in BARNS for i in range(1, ROOMS_PER_BARN + 1) if (b, i) != EMPTY_ROOM]

    def _mortality(self, barn_id: str, idx: int, when: datetime, darab: int, cause: str, valve_n: int | None = None) -> None:
        rid = room_id(barn_id, idx)
        n = valve_n or self.rng.randint(1, VALVES_PER_ROOM)
        vid = valve_id(barn_id, idx, n)
        barn_name = dict(BARNS)[barn_id]
        eid = f"e-{rid}-{int(when.timestamp())}-{self.stats['elhullasok']}"
        self.plan.add(self._farm(S.COL_ELHULLASOK, eid), {
            "telepId": self.farm_id,
            "hizlaldaId": barn_id,
            "hizlaldaName": barn_name,
            "teremId": rid,
            "teremName": room_name(idx),
            "szelepId": vid,
            "szelepName": valve_name(barn_id, idx, n),
            "darab": darab,
            "ok": cause,
            "datum": when,
            "createdAt": when,
        })
        self.stats["elhullasok"] += 1
        # the app also lowers the valve's pig count and logs a valve change
        self._valve_change(barn_id, idx, n, when, -darab)

    def _valve_change(self, barn_id: str, idx: int, n: int, when: datetime, delta: int, abszolut: bool = False) -> None:
        rid = room_id(barn_id, idx)
        vid = valve_id(barn_id, idx, n)
        if abszolut:
            self.valve_counts[vid] = delta
        else:
            self.valve_counts[vid] = max(0, self.valve_counts[vid] + delta)
        self.valve_last[vid] = max(self.valve_last.get(vid, when), when)
        self.room_last_activity[rid] = max(self.room_last_activity.get(rid, when), when)
        if self.clock.local_day(when) == self.today:
            self.valve_today[vid].append(delta)
        doc = {
            "telepId": self.farm_id,
            "hizlaldaId": barn_id,
            "teremId": rid,
            "szelepId": vid,
            "szelepName": valve_name(barn_id, idx, n),
            "modositas": delta,
            "datum": when,
        }
        if abszolut:
            doc["abszolut"] = True
        mid = f"m-{vid}-{int(when.timestamp() * 1000)}-{self.stats['modositasok']}"
        self.plan.add(f"{S.valve_log_collection_path(barn_id, rid, vid)}/{mid}", doc)
        self.stats["modositasok"] += 1

    def _history(self) -> None:
        """Days -20 .. -1 (today is handled separately)."""
        for day_offset in range(HISTORY_DAYS - 1, 0, -1):
            counting_day = day_offset % 7 == 3
            for barn_id, idx in self._populated_rooms():
                rid = room_id(barn_id, idx)
                silent = (barn_id, idx) == SILENT_ROOM and day_offset <= 2
                if silent:
                    continue  # nothing written in the silent room since day -3 (≈3 days)
                # --- mortality ---
                if (barn_id, idx) == CHRONIC_ROOM and day_offset <= 10:
                    deaths = self._poisson(2.5)
                    cause_pool = ["Légzőszervi"] * 5 + ["Ismeretlen"]
                else:
                    deaths = self._poisson(0.45)
                    cause_pool = MORTALITY_CAUSES
                remaining = deaths
                while remaining > 0:
                    d = min(remaining, self.rng.choice([1, 1, 1, 2]))
                    self._mortality(barn_id, idx, self._t_local(day_offset, self._working_hour()), d, self.rng.choice(cause_pool))
                    remaining -= d
                # --- routine valve corrections ---
                if counting_day:
                    base = self._t_local(day_offset, 8.0)
                    for n in range(1, VALVES_PER_ROOM + 1):
                        vid = valve_id(barn_id, idx, n)
                        # counting: set the exact value (small correction from the tracked one)
                        counted = max(0, self.valve_counts[vid] + self.rng.choice([0, 0, 0, 1, -1]))
                        self._valve_change(barn_id, idx, n, base + timedelta(minutes=n * 2), counted, abszolut=True)
                else:
                    for _ in range(self.rng.randint(1, 3)):
                        n = self.rng.randint(1, VALVES_PER_ROOM)
                        self._valve_change(barn_id, idx, n, self._t_local(day_offset, self._working_hour()), self.rng.choice([-2, -1, 1, 2]))

    def _today(self) -> None:
        for barn_id, idx in self._populated_rooms():
            if (barn_id, idx) == SILENT_ROOM:
                continue
            # a bit of routine activity today for every active room (spread over the elapsed day)
            for k in range(self.rng.randint(1, 2)):
                n = self.rng.randint(1, VALVES_PER_ROOM)
                self._valve_change(barn_id, idx, n, self._t_today_before_now(self.rng.uniform(0.15, 0.85)), self.rng.choice([-1, 1]))
            # baseline mortality today (small)
            if (barn_id, idx) not in (SPIKE_ROOM, CHRONIC_ROOM, DECOY_MORTALITY_ROOM):
                for _ in range(self._poisson(0.3)):
                    self._mortality(barn_id, idx, self._t_today_before_now(self.rng.uniform(0.2, 0.8)), 1, self.rng.choice(MORTALITY_CAUSES))

        # 1. clear spike: B-Telep / 5.Terem — 3 + 2 deaths, respiratory
        self._mortality(*SPIKE_ROOM, self._t_today_before_now(0.35), 3, "Légzőszervi", valve_n=4)
        self._mortality(*SPIKE_ROOM, self._t_today_before_now(0.7), 2, "Légzőszervi", valve_n=9)
        # 2. chronic room: 2 + 2 today (candidate, but explained)
        self._mortality(*CHRONIC_ROOM, self._t_today_before_now(0.3), 2, "Légzőszervi", valve_n=2)
        self._mortality(*CHRONIC_ROOM, self._t_today_before_now(0.65), 2, "Légzőszervi", valve_n=11)
        # decoy: 3 today, below threshold
        self._mortality(*DECOY_MORTALITY_ROOM, self._t_today_before_now(0.4), 2, "Hasmenés", valve_n=6)
        self._mortality(*DECOY_MORTALITY_ROOM, self._t_today_before_now(0.75), 1, "Sérülés", valve_n=13)
        # 3. flapping valve A4-07: 8 modifications alternating +2/-2
        for i, delta in enumerate([2, -2, 2, -2, 2, -2, 1, -1]):
            self._valve_change(*FLAP_ROOM, FLAP_VALVE_NO, self._t_today_before_now(0.3 + i * 0.07), delta)

    # ---------------------------------------------------------------- tasks
    def _tasks(self) -> None:
        admin_email = USERS[0][2]
        w1_email = USERS[1][2]
        w2_email = USERS[2][2]

        def task(tid: str, title: str, desc: str, *, barn: str, room_idx: int | None, deadline: datetime | None,
                 assignee: str | None, created_offset_days: float, done: bool = False,
                 completed: datetime | None = None, work_log: list[tuple[float, str]] | None = None) -> None:
            created = self.now - timedelta(days=created_offset_days)
            doc: dict[str, Any] = {
                "title": title,
                "description": desc,
                "category": "Karbantartás" if "javítás" in title.lower() else "Egyéb",
                "done": done,
                "status": S.TASK_STATUS_DONE if done else S.TASK_STATUS_OPEN,
                "deadline": deadline,
                "assigneeEmail": assignee,
                "createdAt": created,
                "createdBy": USERS[0][1],
                "createdByEmail": admin_email,
                "hizlaldaId": barn,
                "hizlaldaNev": dict(BARNS)[barn],
            }
            if room_idx is not None:
                doc["teremId"] = room_id(barn, room_idx)
                doc["teremNev"] = room_name(room_idx)
            if done:
                doc["completedAt"] = completed or (created + timedelta(days=1))
            self.plan.add(self._farm(S.COL_FELADATOK, tid), doc)
            for j, (hours_ago, text) in enumerate(work_log or []):
                self.plan.add(self._farm(S.COL_FELADATOK, tid, S.COL_MUNKAK, f"w{j+1}"), {
                    "description": text,
                    "createdAt": self.now - timedelta(hours=hours_ago),
                })
            self.stats["feladatok"] += 1

        A, B = BARNS[0][0], BARNS[1][0]
        # 5. overdue, untouched
        task("task-overdue", f"Szellőző ventilátor javítása – {room_label(A, 2)}",
             f"A {room_name(2)} keleti ventilátora szakaszosan leáll, cserélni vagy javítani kell.",
             barn=A, room_idx=2, deadline=self._t_local(2, 16.0), assignee=w1_email, created_offset_days=6)
        # 6. due in 6 h, untouched
        task("task-due-soon", f"Itatók ellenőrzése – {room_label(B, 3)}",
             "Két itatószelep gyengén ad vizet, átmosás és nyomás-ellenőrzés.",
             barn=B, room_idx=3, deadline=self.now + timedelta(hours=6), assignee=w2_email, created_offset_days=4)
        # decoy: due in 10 h but in progress
        task("task-in-progress", f"Takarmányadagoló beállítás – {room_label(A, 5)}",
             "Az adagoló 3-as fejénél túladagolás, kalibrálni kell.",
             barn=A, room_idx=5, deadline=self.now + timedelta(hours=10), assignee=w1_email, created_offset_days=2,
             work_log=[(20, "Kalibrálás elkezdve, alkatrész rendelve."), (3, "Alkatrész megjött, délután beszerelem.")])
        # far future
        task("task-later", f"Havi fertőtlenítés – {dict(BARNS)[B]}",
             "Teljes hízlalda fertőtlenítés a szokásos protokoll szerint.",
             barn=B, room_idx=None, deadline=self.now + timedelta(days=5), assignee=w2_email, created_offset_days=1)
        # context for the chronic room
        task("task-chronic", f"Légzőszervi kezelés folytatása – {room_label(A, 3)}",
             "Állatorvosi utasítás szerint 7 napos kúra, napi ellenőrzéssel. Elhullás várhatóan emelkedett marad a kúra végéig.",
             barn=A, room_idx=3, deadline=self.now + timedelta(days=3), assignee=w2_email, created_offset_days=8,
             work_log=[(72, "3. napi adag beadva, 2 állat elkülönítve."), (48, "4. nap: javulás lassú, állatorvos értesítve."),
                       (26, "5. nap: kezelés folytatva."), (5, "6. nap: adag beadva reggel.")])
        # closed history
        task("task-done-1", f"Trágyacsatorna tisztítás – {dict(BARNS)[A]}", "Heti tisztítás.", barn=A, room_idx=None,
             deadline=self._t_local(9, 12.0), assignee=w1_email, created_offset_days=12, done=True,
             completed=self._t_local(9, 10.5))
        task("task-done-2", f"Világítás javítása – {room_label(B, 4)}", "Két fénycső cseréje.", barn=B, room_idx=4,
             deadline=self._t_local(5, 12.0), assignee=w1_email, created_offset_days=7, done=True,
             completed=self._t_local(6, 15.2))

    # ------------------------------------------------------------- finalise
    def _finalise_valves(self) -> None:
        """Write the valve documents with counts and today's roll-up fields."""
        for barn_id, _ in BARNS:
            for idx in range(1, ROOMS_PER_BARN + 1):
                rid = room_id(barn_id, idx)
                for n in range(1, VALVES_PER_ROOM + 1):
                    vid = valve_id(barn_id, idx, n)
                    today_deltas = self.valve_today.get(vid, [])
                    last = self.valve_last.get(vid)
                    doc: dict[str, Any] = {
                        "name": valve_name(barn_id, idx, n),
                        "szelep": self.valve_counts[vid],
                        "modositas": sum(today_deltas),
                        "modositas_szama": len(today_deltas),
                        "telepId": self.farm_id,
                        "hibaAllapot": False,
                    }
                    if last is not None:
                        doc["last_modified"] = last
                        doc["last_modified_day"] = self.clock.day_key(last)
                    self.plan.add(self._farm(S.COL_HIZLALDAK, barn_id, S.COL_TERMEK, rid, S.COL_SZELEPEK, vid), doc)
                    self.stats["szelepek"] += 1

    def _summary(self) -> None:
        pigs_per_room = {}
        for barn_id, _ in BARNS:
            for idx in range(1, ROOMS_PER_BARN + 1):
                pigs_per_room[room_label(barn_id, idx)] = sum(
                    self.valve_counts[valve_id(barn_id, idx, n)] for n in range(1, VALVES_PER_ROOM + 1)
                )
        self.plan.summary = {
            "farm_id": self.farm_id,
            "farm_name": FARM_NAME,
            "now_utc": self.now.isoformat(),
            "today_local": self.today.isoformat(),
            "documents": len(self.plan.writes),
            "counts": dict(self.stats),
            "pigs_per_room": pigs_per_room,
            "planted": {
                "MORTALITY_SPIKE": [room_label(*SPIKE_ROOM), room_label(*CHRONIC_ROOM) + " (chronic — expect NOISE/WATCH)"],
                "VALVE_INSTABILITY": [f"{room_label(*FLAP_ROOM)} valve {valve_name(*FLAP_ROOM, FLAP_VALVE_NO)} (8 changes)"],
                "SILENT_ROOM": [room_label(*SILENT_ROOM)],
                "DEADLINE_RISK": [f"task-overdue ({room_label(BARNS[0][0], 2)}, overdue 2 days)", f"task-due-soon ({room_label(BARNS[1][0], 3)}, due in 6 h)"],
            },
            "decoys": [
                room_label(*DECOY_MORTALITY_ROOM) + ": 3 deaths today (below threshold)",
                room_label(*EMPTY_ROOM) + ": empty room, silent 12 days (ignored)",
                "task-in-progress: due in 10 h but has work-log entries (ignored)",
            ],
        }

    # ------------------------------------------------------------- helpers
    def _poisson(self, lam: float) -> int:
        """Small-λ Poisson sample (Knuth) — deterministic via self.rng."""
        import math
        L = math.exp(-lam)
        k, p = 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= L:
                return k
            k += 1


def build_demo_plan(farm_id: str, clock: Clock, seed: int = 42) -> SeedPlan:
    return DemoDataBuilder(farm_id, clock, seed).build()
