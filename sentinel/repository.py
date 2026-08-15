"""Typed Firestore access for one farm.

The repository is the only module that knows Firestore paths, field names and
query shapes. It hands out the dataclasses from :mod:`sentinel.schema`, with
IDs resolved to names wherever a name exists, so that nothing downstream ever
has to print a raw document ID.

Query shapes worth knowing (each needs an index from ``firestore.indexes.json``):

* valve changes for the farm:  ``collectionGroup('modositasok')
  .where('telepId','==',farm).where('datum','>=',since)``
* valve changes for one room:  ``collectionGroup('modositasok')
  .where('teremId','==',room).where('datum','>=',since).orderBy('datum')``
* mortality for one room:      ``telepek/{farm}/elhullasok
  .where('teremId','==',room).where('datum','>=',since).orderBy('datum')``
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter
from google.cloud.firestore_v1.base_query import BaseQuery

from . import schema as S
from .clock import Clock, clock as default_clock
from .schema import (
    Barn,
    Farm,
    MortalityEvent,
    Permission,
    Room,
    RoomRef,
    Task,
    User,
    Valve,
    ValveChange,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Small conversion helpers (tolerant of the field aliases seen in production)
# --------------------------------------------------------------------------
def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _dt(v: Any) -> datetime | None:
    """Firestore timestamps arrive as tz-aware datetimes; tolerate ISO strings."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------
# Structure snapshot: farm → barns → rooms, resolved once per run
# --------------------------------------------------------------------------
@dataclass
class Structure:
    farm: Farm
    barns: list[Barn]
    rooms: list[Room]
    _by_terem: dict[str, Room] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_terem = {r.id: r for r in self.rooms}

    def room(self, terem_id: str) -> Room | None:
        return self._by_terem.get(terem_id)

    def room_name(self, terem_id: str | None) -> str:
        """Display name for a room ID; never leaks the ID itself."""
        if terem_id and terem_id in self._by_terem:
            return self._by_terem[terem_id].display_name
        return "(unknown room)"

    def rooms_of(self, barn: Barn) -> list[Room]:
        return [r for r in self.rooms if r.barn.id == barn.id]


@dataclass
class DailyCount:
    day: date
    count: int
    events: int = 0  # number of records that day


# --------------------------------------------------------------------------
# The repository
# --------------------------------------------------------------------------
class PigOpsRepository:
    """Read/write access to one farm's PigOps data plus the Sentinel log."""

    def __init__(self, db: firestore.Client, telep_id: str, clock: Clock | None = None) -> None:
        self.db = db
        self.telep_id = telep_id
        self.clock = clock or default_clock
        self._structure: Structure | None = None

    # ------------------------------------------------------------------ paths
    @property
    def farm_ref(self) -> firestore.DocumentReference:
        return self.db.document(S.farm_path(self.telep_id))

    def room_ref(self, room: RoomRef) -> firestore.DocumentReference:
        return self.db.document(S.room_path(room.telep_id, room.hizlalda_id, room.terem_id))

    # -------------------------------------------------------------- structure
    def get_farm(self) -> Farm:
        snap = self.farm_ref.get()
        if not snap.exists:
            raise LookupError(f"Farm document {S.farm_path(self.telep_id)} does not exist")
        d = snap.to_dict() or {}
        return Farm(
            id=snap.id,
            name=_first(d, "nev", "name", default="(unnamed farm)"),
            telep_tipus=d.get("telepTipus", "hizlalda"),
        )

    def get_structure(self, refresh: bool = False) -> Structure:
        """Farm → barns → rooms. Cached for the lifetime of the repository."""
        if self._structure is not None and not refresh:
            return self._structure
        farm = self.get_farm()
        barns: list[Barn] = []
        rooms: list[Room] = []
        for bsnap in self.farm_ref.collection(S.COL_HIZLALDAK).stream():
            bd = bsnap.to_dict() or {}
            barn = Barn(id=bsnap.id, name=_first(bd, "name", "nev", default=bsnap.id), telep_id=self.telep_id)
            barns.append(barn)
            for rsnap in bsnap.reference.collection(S.COL_TERMEK).stream():
                rd = rsnap.to_dict() or {}
                problems = rd.get("teremProblems") or []
                rooms.append(
                    Room(
                        id=rsnap.id,
                        name=_first(rd, "name", "nev", default=rsnap.id),
                        barn=barn,
                        order=_int(rd.get("order")),
                        hizlalasi_nap=rd.get("hizlalasiNap"),
                        becsult_suly=_float(rd.get("becsultSuly")),
                        problem_count=len(problems) if isinstance(problems, list) else _int(rd.get("problemCount")),
                        szelep_hiba_count=_int(rd.get("szelepHibaCount")),
                        raw=rd,
                    )
                )
        barns.sort(key=lambda b: b.name)
        rooms.sort(key=lambda r: (r.barn.name, r.order, r.name))
        self._structure = Structure(farm=farm, barns=barns, rooms=rooms)
        return self._structure

    def resolve_room(self, terem_id: str) -> Room:
        room = self.get_structure().room(terem_id)
        if room is None:
            raise LookupError(f"Room {terem_id!r} is not part of farm {self.telep_id!r}")
        return room

    # ----------------------------------------------------------------- valves
    def get_valves(self, room: RoomRef) -> list[Valve]:
        out: list[Valve] = []
        for vsnap in self.room_ref(room).collection(S.COL_SZELEPEK).stream():
            vd = vsnap.to_dict() or {}
            out.append(
                Valve(
                    id=vsnap.id,
                    name=str(_first(vd, "name", "nev", default=vsnap.id)),
                    room=room,
                    szelep=_int(vd.get("szelep")),
                    modositas=_int(vd.get("modositas")),
                    modositas_szama=_int(vd.get("modositas_szama")),
                    last_modified=_dt(vd.get("last_modified")),
                    last_modified_day=vd.get("last_modified_day"),
                    hiba_allapot=bool(vd.get("hibaAllapot", False)),
                    hiba_uzenet=vd.get("hibaUzenet"),
                )
            )
        out.sort(key=lambda v: (len(v.name), v.name))  # "101" < "102" < ... numeric-ish
        return out

    def get_all_valves(self) -> dict[str, list[Valve]]:
        """{terem_id: [valves]} for every room of the farm (path-scoped reads)."""
        return {room.id: self.get_valves(room.ref) for room in self.get_structure().rooms}

    # -------------------------------------------------------------- mortality
    def _mortality_query(self, since: datetime, until: datetime | None, terem_id: str | None) -> BaseQuery:
        q: BaseQuery = self.farm_ref.collection(S.COL_ELHULLASOK)
        if terem_id:
            q = q.where(filter=FieldFilter("teremId", "==", terem_id))
        q = q.where(filter=FieldFilter("datum", ">=", since))
        if until is not None:
            q = q.where(filter=FieldFilter("datum", "<", until))
        return q.order_by("datum")

    def get_mortality_events(
        self, since: datetime, until: datetime | None = None, terem_id: str | None = None
    ) -> list[MortalityEvent]:
        structure = self.get_structure()
        out: list[MortalityEvent] = []
        for snap in self._mortality_query(since, until, terem_id).stream():
            d = snap.to_dict() or {}
            tid = _first(d, "teremId", "terem")
            room = structure.room(tid) if tid else None
            hid = _first(d, "hizlaldaId", "hizlalda", default=room.barn.id if room else "")
            out.append(
                MortalityEvent(
                    id=snap.id,
                    room=RoomRef(self.telep_id, hid, tid or ""),
                    terem_name=_first(d, "teremName", default=room.name if room else "(unknown room)"),
                    hizlalda_name=_first(d, "hizlaldaName", default=room.barn.name if room else "(unknown barn)"),
                    szelep_name=_first(d, "szelepName"),
                    darab=_int(_first(d, "darab", "mennyiseg", "count", default=1), default=1),
                    ok=_first(d, "ok", "oka", "cause"),
                    datum=_dt(d.get("datum")) or self.clock.now(),
                    suly=_float(d.get("suly")),
                )
            )
        return out

    def get_mortality_history(self, terem_id: str, days: int) -> list[DailyCount]:
        """Zero-filled daily series for the last ``days`` farm-local days (today included)."""
        today = self.clock.today()
        first_day = today - timedelta(days=days - 1)
        since, _ = self.clock.day_bounds(first_day)
        per_day: dict[date, DailyCount] = {
            first_day + timedelta(days=i): DailyCount(first_day + timedelta(days=i), 0) for i in range(days)
        }
        for ev in self.get_mortality_events(since, terem_id=terem_id):
            d = self.clock.local_day(ev.datum)
            if d in per_day:
                per_day[d].count += ev.darab
                per_day[d].events += 1
        return [per_day[k] for k in sorted(per_day)]

    def get_mortality_today_by_room(self) -> dict[str, list[MortalityEvent]]:
        start, end = self.clock.day_bounds(self.clock.today())
        by_room: dict[str, list[MortalityEvent]] = defaultdict(list)
        for ev in self.get_mortality_events(start, end):
            by_room[ev.room.terem_id].append(ev)
        return dict(by_room)

    # ---------------------------------------------------------- valve changes
    def _valve_change(self, snap: firestore.DocumentSnapshot) -> ValveChange:
        d = snap.to_dict() or {}
        return ValveChange(
            id=snap.id,
            room=RoomRef(d.get("telepId", self.telep_id), d.get("hizlaldaId", ""), d.get("teremId", "")),
            szelep_id=d.get("szelepId", ""),
            szelep_name=str(d.get("szelepName", "?")),
            modositas=_int(d.get("modositas")),
            datum=_dt(d.get("datum")) or self.clock.now(),
            abszolut=bool(d.get("abszolut", False)),
        )

    def get_valve_changes(self, since: datetime, until: datetime | None = None) -> list[ValveChange]:
        """All valve modifications of the farm since ``since`` (collection-group query)."""
        q: BaseQuery = (
            self.db.collection_group(S.COL_MODOSITASOK)
            .where(filter=FieldFilter("telepId", "==", self.telep_id))
            .where(filter=FieldFilter("datum", ">=", since))
        )
        if until is not None:
            q = q.where(filter=FieldFilter("datum", "<", until))
        return [self._valve_change(s) for s in q.stream()]

    def get_valve_log(self, terem_id: str, days: int) -> list[ValveChange]:
        """Valve modifications of one room over the last ``days`` days, oldest first."""
        first_day = self.clock.today() - timedelta(days=days - 1)
        since, _ = self.clock.day_bounds(first_day)
        q = (
            self.db.collection_group(S.COL_MODOSITASOK)
            .where(filter=FieldFilter("teremId", "==", terem_id))
            .where(filter=FieldFilter("datum", ">=", since))
            .order_by("datum")
        )
        return [self._valve_change(s) for s in q.stream() if s.get("telepId") == self.telep_id]

    def get_valve_changes_today_by_room(self) -> dict[str, list[ValveChange]]:
        start, end = self.clock.day_bounds(self.clock.today())
        by_room: dict[str, list[ValveChange]] = defaultdict(list)
        for ch in self.get_valve_changes(start, end):
            by_room[ch.room.terem_id].append(ch)
        return dict(by_room)

    # ------------------------------------------------------------------ tasks
    def _task(self, snap: firestore.DocumentSnapshot) -> Task:
        d = snap.to_dict() or {}
        return Task(
            id=snap.id,
            telep_id=self.telep_id,
            title=_first(d, "title", "cim", default="(untitled)"),
            description=_first(d, "description", "leiras", default=""),
            done=bool(d.get("done", False)),
            status=_first(d, "status", "statusz"),
            deadline=_dt(_first(d, "hataridoAt", "deadline", "hatarido", "dueDate")),
            assignee_email=_first(d, "assigneeEmail", "assignee", "felelos"),
            created_at=_dt(d.get("createdAt")),
            created_by=d.get("createdBy"),
            hizlalda_id=d.get("hizlaldaId"),
            hizlalda_nev=_first(d, "hizlaldaNev", "hizlaldaName"),
            terem_id=d.get("teremId"),
            terem_nev=_first(d, "teremNev", "teremName"),
            completed_at=_dt(_first(d, "completedAt", "lezarvaAt")),
            category=d.get("category"),
            source=d.get("source"),
            severity=d.get("severity"),
            signal_type=d.get("signalType"),
            sentinel_run_id=d.get("sentinelRunId"),
            escalated_at=_dt(d.get("escalatedAt")),
            escalation_count=_int(d.get("escalationCount"), 0),
            sentinel_outcome_logged_at=_dt(d.get("sentinelOutcomeLoggedAt")),
            sentinel_notified_at=_dt(d.get("sentinelNotifiedAt")),
            sentinel_notify_count=_int(d.get("sentinelNotifyCount"), 0),
        )

    def get_open_tasks(self, terem_id: str | None = None) -> list[Task]:
        q: BaseQuery = self.farm_ref.collection(S.COL_FELADATOK).where(filter=FieldFilter("done", "==", False))
        tasks = [self._task(s) for s in q.stream()]
        if terem_id:
            tasks = [t for t in tasks if t.terem_id == terem_id]
        tasks.sort(key=lambda t: (t.deadline is None, t.deadline or datetime.max.replace(tzinfo=self.clock.now().tzinfo)))
        return tasks

    def get_sentinel_tasks(self, include_done: bool = True) -> list[Task]:
        q: BaseQuery = self.farm_ref.collection(S.COL_FELADATOK).where(
            filter=FieldFilter("source", "==", S.TASK_SOURCE_SENTINEL)
        )
        tasks = [self._task(s) for s in q.stream()]
        if not include_done:
            tasks = [t for t in tasks if not t.done]
        return tasks

    def get_task_work_log_count(self, task_id: str) -> int:
        col = self.farm_ref.collection(S.COL_FELADATOK).document(task_id).collection(S.COL_MUNKAK)
        return sum(1 for _ in col.limit(50).stream())

    def create_task(
        self,
        *,
        room: Room,
        title: str,
        description: str,
        assignee_email: str | None,
        severity: str,
        signal_type: str,
        run_id: str,
        deadline: datetime,
        signal_key: str,
    ) -> Task:
        """Create a task exactly the way the PigOps app expects it, plus Sentinel markers."""
        doc = {
            # --- fields the production app reads ---
            "title": title,
            "description": description,
            "category": "Sentinel",
            "done": False,
            "status": S.TASK_STATUS_OPEN,
            "deadline": deadline,
            "assigneeEmail": assignee_email,
            "createdAt": firestore.SERVER_TIMESTAMP,
            "createdBy": "PigOps Sentinel",
            "createdByEmail": "sentinel@pigops.local",
            "hizlaldaId": room.barn.id,
            "hizlaldaNev": room.barn.name,
            # --- extras (ignored by the app, used by the Sentinel) ---
            "teremId": room.id,
            "teremNev": room.name,
            "source": S.TASK_SOURCE_SENTINEL,
            "severity": severity,
            "signalType": signal_type,
            "signalKey": signal_key,
            "sentinelRunId": run_id,
        }
        ref = self.farm_ref.collection(S.COL_FELADATOK).document()
        ref.set(doc)
        return self._task(ref.get())

    def update_task(self, task_id: str, **fields: Any) -> None:
        self.farm_ref.collection(S.COL_FELADATOK).document(task_id).update(fields)

    def mark_task_notified(self, task_id: str, at: datetime) -> None:
        """Stamp a task the Sentinel just notified about (extra fields, ignored by the app):
        the scanner and the actor use it to not nag about the same task every run."""
        self.update_task(task_id, sentinelNotifiedAt=at, sentinelNotifyCount=firestore.Increment(1))

    def escalate_task(self, task_id: str, severity: str, at: datetime) -> None:
        """Follow-up: raise the severity and stamp the escalation (the app keeps showing the task)."""
        self.update_task(task_id, severity=severity, escalatedAt=at, escalationCount=firestore.Increment(1))

    def mark_task_outcome_logged(self, task_id: str, at: datetime) -> None:
        self.update_task(task_id, sentinelOutcomeLoggedAt=at)

    def find_recent_sentinel_task(self, terem_id: str, signal_type: str, within_hours: int) -> Task | None:
        """Dedup lookup: an open Sentinel task for the same room+signal, or any
        Sentinel task for it created within the dedup window."""
        cutoff = self.clock.now() - timedelta(hours=within_hours)
        candidates = [
            t for t in self.get_sentinel_tasks(include_done=True)
            if t.terem_id == terem_id and t.signal_type == signal_type
        ]
        for t in candidates:
            if not t.done:
                return t
            if t.created_at and t.created_at >= cutoff:
                return t
        return None

    # ---------------------------------------------------------- people / roles
    def get_farm_permissions(self) -> list[Permission]:
        out = []
        for snap in self.farm_ref.collection(S.COL_JOGOSULTSAGOK).stream():
            d = snap.to_dict() or {}
            out.append(Permission(uid=d.get("uid", snap.id), role=d.get("role", S.ROLE_USER), created_at=_dt(d.get("createdAt"))))
        return out

    def get_user(self, uid: str) -> User | None:
        snap = self.db.collection(S.COL_USERS).document(uid).get()
        if not snap.exists:
            return None
        d = snap.to_dict() or {}
        return User(
            uid=snap.id,
            email=d.get("email"),
            display_name=_first(d, "displayName", "name"),
            role=d.get("role"),
            fcm_token=d.get("fcmToken"),
        )

    def get_farm_manager(self) -> tuple[User | None, str | None]:
        """The person responsible for the farm.

        Primary: the farm-level ``admin`` in ``jogosultsagok`` (oldest first —
        the founder of the farm). Fallback: a ``superadmin`` on the farm, then
        any global admin found among the farm's users. Returns ``(user, note)``
        where ``note`` explains a fallback, or ``(None, note)`` if nobody fits.
        """
        perms = self.get_farm_permissions()
        perms.sort(key=lambda p: (p.created_at is None, p.created_at or self.clock.now()))
        for role in (S.ROLE_ADMIN, S.ROLE_SUPERADMIN):
            for p in perms:
                if p.role == role:
                    u = self.get_user(p.uid)
                    if u:
                        note = None if role == S.ROLE_ADMIN else "no farm admin found; assigned to a superadmin"
                        return u, note
        for p in perms:
            u = self.get_user(p.uid)
            if u and u.role in S.FARM_MANAGER_ROLES:
                return u, "no farm-level admin; fell back to a global admin who has access to the farm"
        return None, "no responsible person found on the farm — task left unassigned"

    # -------------------------------------------------------- sentinel log/runs
    def write_log(self, entry: dict[str, Any]) -> str:
        entry = dict(entry)
        entry.setdefault("timestamp", firestore.SERVER_TIMESTAMP)
        ref = self.db.collection(S.COL_SENTINEL_LOG).document()
        ref.set(entry)
        return ref.id

    def get_recent_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        q = self.db.collection(S.COL_SENTINEL_LOG).order_by("timestamp", direction=firestore.Query.DESCENDING).limit(limit)
        return [{"id": s.id, **(s.to_dict() or {})} for s in q.stream()]

    def upsert_run(self, run_id: str, **fields: Any) -> None:
        self.db.collection(S.COL_SENTINEL_RUNS).document(run_id).set(fields, merge=True)

    def get_recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        q = self.db.collection(S.COL_SENTINEL_RUNS).order_by("startedAt", direction=firestore.Query.DESCENDING).limit(limit)
        return [{"id": s.id, **(s.to_dict() or {})} for s in q.stream()]


# --------------------------------------------------------------------------
# Convenience for scripts
# --------------------------------------------------------------------------
def batched(items: Iterable[Any], size: int = 400) -> Iterable[list[Any]]:
    """Yield lists of at most ``size`` items (Firestore batches cap at 500 writes)."""
    chunk: list[Any] = []
    for it in items:
        chunk.append(it)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
