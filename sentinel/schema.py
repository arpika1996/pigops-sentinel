"""The PigOps Firestore schema, mirrored field-for-field.

PigOps is a Hungarian farm-management platform; its Firestore documents use
Hungarian field names. The Sentinel reads and writes the *same* documents the
production app does, so this module is the single place where those names are
spelled out. Everything else in the codebase goes through the typed helpers
here — never through string literals.

Document tree (only the parts the Sentinel touches)::

    telepek/{telepId}                                  farm      {nev, telepTipus, ...}
      hizlaldak/{hizlaldaId}                           barn      {name}
        termek/{teremId}                               room      {name, order, hizlalasiNap, becsultSuly, teremProblems[], ...}
          szelepek/{szelepId}                          valve     {name, szelep, modositas, modositas_szama, last_modified,
                                                                  last_modified_day, telepId, hibaAllapot, ...}
      elhullasok/{id}                                  mortality {telepId, hizlaldaId, hizlaldaName, teremId, teremName,
                                                                  szelepId, szelepName, darab, ok, datum, createdAt}
      feladatok/{id}                                   task      {title, description, category, done, status, deadline,
                                                                  assigneeEmail, createdAt, createdBy, createdByEmail,
                                                                  hizlaldaId, hizlaldaNev, completedAt?}
        munkak/{id}                                    work log  {description, createdAt}
      jogosultsagok/{uid}                              per-farm role {uid, role: admin|superadmin|user|reader, ...}
      elhullas_okok/{id}                               cause list {nev}
    naplo/{hizlaldaId}/termek/{teremId}/szelepek/{szelepId}/modositasok/{id}
                                                       valve change {telepId, hizlaldaId, teremId, szelepId, szelepName,
                                                                     modositas, abszolut?, datum}
    users/{uid}                                        {uid, email, displayName, role, fcmToken, telepId, telepIds[]}
    sentinelLog/{logId}                                the Sentinel's own decision log (see SPEC.md §5)
    sentinelRuns/{runId}                               one doc per run: {startedAt, finishedAt, status, phase,
                                                                         currentTarget, candidates, tasksCreated, error}

Notes that matter when writing seed data:

* Farm name field is ``nev``; barn/room/valve name field is ``name``.
* The valve change log lives in a *separate root tree* (``naplo/...``) that
  carries no farm ID in its path — every document has ``telepId`` in its
  body and is only ever queried through ``collectionGroup('modositasok')``.
* Every valve document carries a denormalised ``telepId``.
* ``feladatok.done`` (bool) is the authoritative task status.
* Timestamps are real Firestore timestamps (tz-aware ``datetime`` in Python).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# --------------------------------------------------------------------------
# Collection names (top level and sub-collections)
# --------------------------------------------------------------------------
COL_TELEPEK = "telepek"
COL_HIZLALDAK = "hizlaldak"
COL_TERMEK = "termek"
COL_SZELEPEK = "szelepek"
COL_ELHULLASOK = "elhullasok"
COL_ELHULLAS_OKOK = "elhullas_okok"
COL_FELADATOK = "feladatok"
COL_MUNKAK = "munkak"
COL_JOGOSULTSAGOK = "jogosultsagok"
COL_NAPLO = "naplo"
COL_MODOSITASOK = "modositasok"
COL_USERS = "users"
COL_SENTINEL_LOG = "sentinelLog"  # one doc per phase-2..5 outcome (SPEC §5)
COL_SENTINEL_RUNS = "sentinelRuns"  # one doc per run: live phase/status for the console

# Marker the Sentinel stamps on every task it creates (extra fields — the
# production app ignores unknown fields, so this is backwards compatible).
TASK_SOURCE_SENTINEL = "sentinel"

# Farm-level roles as validated by the production access-control code.
ROLE_ADMIN = "admin"
ROLE_SUPERADMIN = "superadmin"
ROLE_USER = "user"
ROLE_READER = "reader"
FARM_MANAGER_ROLES = (ROLE_ADMIN, ROLE_SUPERADMIN)

TASK_STATUS_OPEN = "folyamatban"
TASK_STATUS_DONE = "kész"


# --------------------------------------------------------------------------
# Path helpers
# --------------------------------------------------------------------------
def farm_path(telep_id: str) -> str:
    return f"{COL_TELEPEK}/{telep_id}"


def barn_path(telep_id: str, hizlalda_id: str) -> str:
    return f"{farm_path(telep_id)}/{COL_HIZLALDAK}/{hizlalda_id}"


def room_path(telep_id: str, hizlalda_id: str, terem_id: str) -> str:
    return f"{barn_path(telep_id, hizlalda_id)}/{COL_TERMEK}/{terem_id}"


def valve_path(telep_id: str, hizlalda_id: str, terem_id: str, szelep_id: str) -> str:
    return f"{room_path(telep_id, hizlalda_id, terem_id)}/{COL_SZELEPEK}/{szelep_id}"


def valve_log_collection_path(hizlalda_id: str, terem_id: str, szelep_id: str) -> str:
    """The `naplo/...` tree — note: NO farm ID in the path."""
    return (
        f"{COL_NAPLO}/{hizlalda_id}/{COL_TERMEK}/{terem_id}"
        f"/{COL_SZELEPEK}/{szelep_id}/{COL_MODOSITASOK}"
    )


# --------------------------------------------------------------------------
# Typed in-memory views. IDs are kept for lookups; names are what humans see.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RoomRef:
    """Fully-qualified room reference — enough to build any path."""

    telep_id: str
    hizlalda_id: str
    terem_id: str


@dataclass
class Farm:
    id: str
    name: str  # `nev`
    telep_tipus: str = "hizlalda"


@dataclass
class Barn:
    id: str
    name: str  # `name`
    telep_id: str


@dataclass
class Room:
    id: str
    name: str  # `name`
    barn: Barn
    order: int = 0
    hizlalasi_nap: int | None = None  # `hizlalasiNap`
    becsult_suly: float | None = None  # `becsultSuly`
    problem_count: int = 0  # len(teremProblems)
    szelep_hiba_count: int = 0  # `szelepHibaCount`
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def ref(self) -> RoomRef:
        return RoomRef(self.barn.telep_id, self.barn.id, self.id)

    @property
    def display_name(self) -> str:
        """`"B-Telep / 5.Terem"` — the only form that may reach a human."""
        return f"{self.barn.name} / {self.name}"


@dataclass
class Valve:
    id: str
    name: str  # `name` — the valve number as a string, e.g. "107"
    room: RoomRef
    szelep: int = 0  # current pig count on the valve
    modositas: int = 0  # net delta today
    modositas_szama: int = 0  # modification count today
    last_modified: datetime | None = None
    last_modified_day: str | None = None  # yyyy-MM-dd, farm-local
    hiba_allapot: bool = False
    hiba_uzenet: str | None = None


@dataclass
class MortalityEvent:
    id: str
    room: RoomRef
    terem_name: str
    hizlalda_name: str
    szelep_name: str | None
    darab: int
    ok: str | None  # cause
    datum: datetime
    suly: float | None = None


@dataclass
class ValveChange:
    id: str
    room: RoomRef
    szelep_id: str
    szelep_name: str
    modositas: int
    datum: datetime
    abszolut: bool = False


@dataclass
class Task:
    id: str
    telep_id: str
    title: str
    description: str
    done: bool
    status: str | None
    deadline: datetime | None
    assignee_email: str | None
    created_at: datetime | None
    created_by: str | None
    hizlalda_id: str | None
    hizlalda_nev: str | None
    terem_id: str | None = None
    terem_nev: str | None = None
    completed_at: datetime | None = None
    category: str | None = None
    # Sentinel-specific extras (absent on tasks created by humans)
    source: str | None = None
    severity: str | None = None
    signal_type: str | None = None
    sentinel_run_id: str | None = None
    escalated_at: datetime | None = None
    sentinel_notified_at: datetime | None = None  # last DEADLINE_RISK notification the Sentinel sent about it
    sentinel_notify_count: int = 0
    work_log_count: int | None = None  # filled lazily by the repository

    @property
    def is_sentinel_task(self) -> bool:
        return self.source == TASK_SOURCE_SENTINEL


@dataclass
class User:
    uid: str
    email: str | None
    display_name: str | None
    role: str | None = None
    fcm_token: str | None = None


@dataclass
class Permission:
    uid: str
    role: str
    created_at: datetime | None = None
