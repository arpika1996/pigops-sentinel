"""Build a :class:`sentinel.scanner.FarmSnapshot` straight from a seed plan.

This is the in-memory stand-in for ``load_snapshot(repo)``: it reconstructs
the same dataclasses the repository would return, so the scanner rules can be
tested without any Firestore.
"""

from __future__ import annotations

from collections import defaultdict

from sentinel import schema as S
from sentinel.clock import Clock
from sentinel.repository import Structure
from sentinel.scanner import FarmSnapshot
from sentinel.schema import Barn, Farm, MortalityEvent, Room, RoomRef, Task, User, Valve, ValveChange
from sentinel.seed.demo_data import SeedPlan


def _segs(path: str) -> list[str]:
    return path.split("/")


def snapshot_from_plan(plan: SeedPlan, clock: Clock, farm_id: str = "demo-farm") -> FarmSnapshot:
    farm = None
    barns: dict[str, Barn] = {}
    rooms: dict[str, Room] = {}
    valves: dict[str, list[Valve]] = defaultdict(list)
    mortality_today: dict[str, list[MortalityEvent]] = defaultdict(list)
    changes_today: dict[str, list[ValveChange]] = defaultdict(list)
    tasks: dict[str, Task] = {}
    worklog: dict[str, int] = defaultdict(int)
    users: dict[str, User] = {}
    perms: list[str] = []
    today = clock.today()

    for w in plan.writes:
        segs = _segs(w.path)
        d = w.data
        if segs == [S.COL_TELEPEK, farm_id]:
            farm = Farm(id=farm_id, name=d["nev"], telep_tipus=d.get("telepTipus", "hizlalda"))
        elif len(segs) == 4 and segs[2] == S.COL_HIZLALDAK:
            barns[segs[3]] = Barn(id=segs[3], name=d["name"], telep_id=farm_id)
        elif len(segs) == 6 and segs[4] == S.COL_TERMEK:
            rooms[segs[5]] = Room(
                id=segs[5], name=d["name"], barn=barns[segs[3]], order=d.get("order", 0),
                hizlalasi_nap=d.get("hizlalasiNap"), becsult_suly=d.get("becsultSuly"),
                problem_count=len(d.get("teremProblems") or []), szelep_hiba_count=d.get("szelepHibaCount", 0), raw=d,
            )
        elif len(segs) == 8 and segs[6] == S.COL_SZELEPEK:
            ref = RoomRef(farm_id, segs[3], segs[5])
            valves[segs[5]].append(Valve(
                id=segs[7], name=d["name"], room=ref, szelep=d["szelep"], modositas=d.get("modositas", 0),
                modositas_szama=d.get("modositas_szama", 0), last_modified=d.get("last_modified"),
                last_modified_day=d.get("last_modified_day"), hiba_allapot=d.get("hibaAllapot", False),
            ))
        elif len(segs) == 4 and segs[2] == S.COL_ELHULLASOK:
            if clock.local_day(d["datum"]) == today:
                mortality_today[d["teremId"]].append(MortalityEvent(
                    id=segs[3], room=RoomRef(farm_id, d["hizlaldaId"], d["teremId"]), terem_name=d["teremName"],
                    hizlalda_name=d["hizlaldaName"], szelep_name=d.get("szelepName"), darab=d["darab"], ok=d.get("ok"),
                    datum=d["datum"], suly=d.get("suly"),
                ))
        elif segs[0] == S.COL_NAPLO and segs[-2] == S.COL_MODOSITASOK:
            if clock.local_day(d["datum"]) == today:
                changes_today[d["teremId"]].append(ValveChange(
                    id=segs[-1], room=RoomRef(d["telepId"], d["hizlaldaId"], d["teremId"]), szelep_id=d["szelepId"],
                    szelep_name=d["szelepName"], modositas=d["modositas"], datum=d["datum"], abszolut=d.get("abszolut", False),
                ))
        elif len(segs) == 4 and segs[2] == S.COL_FELADATOK:
            tasks[segs[3]] = Task(
                id=segs[3], telep_id=farm_id, title=d["title"], description=d.get("description", ""), done=d["done"],
                status=d.get("status"), deadline=d.get("deadline"), assignee_email=d.get("assigneeEmail"),
                created_at=d.get("createdAt"), created_by=d.get("createdBy"), hizlalda_id=d.get("hizlaldaId"),
                hizlalda_nev=d.get("hizlaldaNev"), terem_id=d.get("teremId"), terem_nev=d.get("teremNev"),
                completed_at=d.get("completedAt"), category=d.get("category"), source=d.get("source"),
                severity=d.get("severity"), signal_type=d.get("signalType"), sentinel_run_id=d.get("sentinelRunId"),
                sentinel_notified_at=d.get("sentinelNotifiedAt"), sentinel_notify_count=d.get("sentinelNotifyCount", 0),
            )
        elif len(segs) == 6 and segs[4] == S.COL_MUNKAK:
            worklog[segs[3]] += 1
        elif segs[0] == S.COL_USERS and len(segs) == 2:
            users[segs[1]] = User(uid=segs[1], email=d.get("email"), display_name=d.get("displayName"), role=d.get("role"), fcm_token=d.get("fcmToken"))
        elif len(segs) == 4 and segs[2] == S.COL_JOGOSULTSAGOK:
            perms.append(segs[3])

    assert farm is not None
    room_list = sorted(rooms.values(), key=lambda r: (r.barn.name, r.order, r.name))
    structure = Structure(farm=farm, barns=sorted(barns.values(), key=lambda b: b.name), rooms=room_list)
    open_tasks = [t for t in tasks.values() if not t.done]
    for t in open_tasks:
        t.work_log_count = worklog.get(t.id, 0)
    users_by_email = {u.email.lower(): u for uid, u in users.items() if uid in perms and u.email}
    return FarmSnapshot(structure, dict(valves), dict(mortality_today), dict(changes_today), open_tasks, users_by_email, clock.now(), clock.tz)


# --------------------------------------------------------------------------
# A repository stand-in for the agent tools (history queries), built from a plan
# --------------------------------------------------------------------------
class FakeRepository:
    """Implements the read methods the agent tools use, over a seed plan."""

    def __init__(self, plan: SeedPlan, clock: Clock, farm_id: str = "demo-farm") -> None:
        from datetime import date, timedelta

        from sentinel.repository import DailyCount

        self.clock = clock
        self.telep_id = farm_id
        self._DailyCount = DailyCount
        self._timedelta = timedelta
        self.mortality: list[MortalityEvent] = []
        self.changes: list[ValveChange] = []
        self.worklog: dict[str, int] = defaultdict(int)
        for w in plan.writes:
            segs = _segs(w.path)
            d = w.data
            if len(segs) == 4 and segs[2] == S.COL_ELHULLASOK:
                self.mortality.append(MortalityEvent(
                    id=segs[3], room=RoomRef(farm_id, d["hizlaldaId"], d["teremId"]), terem_name=d["teremName"],
                    hizlalda_name=d["hizlaldaName"], szelep_name=d.get("szelepName"), darab=d["darab"], ok=d.get("ok"),
                    datum=d["datum"], suly=d.get("suly"),
                ))
            elif segs[0] == S.COL_NAPLO and segs[-2] == S.COL_MODOSITASOK:
                self.changes.append(ValveChange(
                    id=segs[-1], room=RoomRef(d["telepId"], d["hizlaldaId"], d["teremId"]), szelep_id=d["szelepId"],
                    szelep_name=d["szelepName"], modositas=d["modositas"], datum=d["datum"], abszolut=d.get("abszolut", False),
                ))
            elif len(segs) == 6 and segs[4] == S.COL_MUNKAK:
                self.worklog[segs[3]] += 1
        self.mortality.sort(key=lambda e: e.datum)
        self.changes.sort(key=lambda c: c.datum)
        self.calls: list[tuple] = []
        # tasks / people, the same way the snapshot builder sees them
        snap = snapshot_from_plan(plan, clock, farm_id)
        self.structure = snap.structure
        self.tasks: dict[str, Task] = {}
        self.users: dict[str, User] = {}
        self.perms: dict[str, tuple[str, object]] = {}   # uid -> (role, createdAt)
        for w in plan.writes:
            segs = _segs(w.path)
            d = w.data
            if len(segs) == 4 and segs[2] == S.COL_FELADATOK:
                self.tasks[segs[3]] = Task(
                    id=segs[3], telep_id=farm_id, title=d["title"], description=d.get("description", ""), done=d["done"],
                    status=d.get("status"), deadline=d.get("deadline"), assignee_email=d.get("assigneeEmail"),
                    created_at=d.get("createdAt"), created_by=d.get("createdBy"), hizlalda_id=d.get("hizlaldaId"),
                    hizlalda_nev=d.get("hizlaldaNev"), terem_id=d.get("teremId"), terem_nev=d.get("teremNev"),
                    completed_at=d.get("completedAt"), category=d.get("category"), source=d.get("source"),
                    severity=d.get("severity"), signal_type=d.get("signalType"), sentinel_run_id=d.get("sentinelRunId"),
                    sentinel_notified_at=d.get("sentinelNotifiedAt"), sentinel_notify_count=d.get("sentinelNotifyCount", 0),
                    escalated_at=d.get("escalatedAt"), escalation_count=d.get("escalationCount", 0),
                    sentinel_outcome_logged_at=d.get("sentinelOutcomeLoggedAt"),
                )
            elif segs[0] == S.COL_USERS and len(segs) == 2:
                self.users[segs[1]] = User(uid=segs[1], email=d.get("email"), display_name=d.get("displayName"),
                                           role=d.get("role"), fcm_token=d.get("fcmToken"))
            elif len(segs) == 4 and segs[2] == S.COL_JOGOSULTSAGOK:
                self.perms[segs[3]] = (d.get("role", S.ROLE_USER), d.get("createdAt"))
        for t in self.tasks.values():          # the real repository fills this lazily; the scanner relies on it
            t.work_log_count = self.worklog.get(t.id, 0)
        self._task_seq = 0

    def get_mortality_events(self, since, until=None, terem_id=None):
        self.calls.append(("get_mortality_events", since, until, terem_id))
        return [e for e in self.mortality if e.datum >= since and (until is None or e.datum < until) and (terem_id is None or e.room.terem_id == terem_id)]

    def get_mortality_history(self, terem_id: str, days: int):
        self.calls.append(("get_mortality_history", terem_id, days))
        today = self.clock.today()
        first = today - self._timedelta(days=days - 1)
        per_day = {first + self._timedelta(days=i): self._DailyCount(first + self._timedelta(days=i), 0) for i in range(days)}
        for e in self.mortality:
            if e.room.terem_id != terem_id:
                continue
            d = self.clock.local_day(e.datum)
            if d in per_day:
                per_day[d].count += e.darab
                per_day[d].events += 1
        return [per_day[k] for k in sorted(per_day)]

    def get_valve_log(self, terem_id: str, days: int):
        self.calls.append(("get_valve_log", terem_id, days))
        first = self.clock.today() - self._timedelta(days=days - 1)
        since = self.clock.day_bounds(first)[0]
        return [c for c in self.changes if c.room.terem_id == terem_id and c.datum >= since]

    def get_task_work_log_count(self, task_id: str) -> int:
        self.calls.append(("get_task_work_log_count", task_id))
        return self.worklog.get(task_id, 0)

    # ---------------------------------------------------------- sentinel log/runs
    # (in-memory mirrors of PigOpsRepository.write_log / upsert_run / get_recent_*)
    def write_log(self, entry: dict) -> str:
        if not hasattr(self, "logs"):
            self.logs = []
        log_id = f"log-{len(self.logs) + 1:04d}"
        self.logs.append({"id": log_id, **dict(entry)})
        return log_id

    def get_recent_logs(self, limit: int = 100) -> list[dict]:
        logs = getattr(self, "logs", [])
        return sorted(logs, key=lambda e: e["timestamp"], reverse=True)[:limit]

    def upsert_run(self, run_id: str, **fields) -> None:
        if not hasattr(self, "runs"):
            self.runs = {}
        self.runs.setdefault(run_id, {}).update(fields)
        self.calls.append(("upsert_run", run_id, dict(fields)))

    def get_recent_runs(self, limit: int = 20) -> list[dict]:
        runs = getattr(self, "runs", {})
        docs = [{"id": rid, **d} for rid, d in runs.items()]
        return sorted(docs, key=lambda d: d.get("startedAt") or self.clock.now(), reverse=True)[:limit]


    # ---------------------------------------------------------------- tasks
    # (mirrors PigOpsRepository.get_open_tasks / get_sentinel_tasks / create_task /
    #  update_task / mark_task_notified / find_recent_sentinel_task)
    def get_open_tasks(self, terem_id: str | None = None) -> list[Task]:
        return [t for t in self.tasks.values() if not t.done and (terem_id is None or t.terem_id == terem_id)]

    def get_sentinel_tasks(self, include_done: bool = True) -> list[Task]:
        return [t for t in self.tasks.values() if t.is_sentinel_task and (include_done or not t.done)]

    def create_task(self, *, room, title, description, assignee_email, severity, signal_type, run_id, deadline, signal_key) -> Task:
        self._task_seq += 1
        tid = f"sentinel-task-{self._task_seq}"
        t = Task(
            id=tid, telep_id=self.telep_id, title=title, description=description, done=False, status=S.TASK_STATUS_OPEN,
            deadline=deadline, assignee_email=assignee_email, created_at=self.clock.now(), created_by="PigOps Sentinel",
            hizlalda_id=room.barn.id, hizlalda_nev=room.barn.name, terem_id=room.id, terem_nev=room.name,
            category="Sentinel", source=S.TASK_SOURCE_SENTINEL, severity=severity, signal_type=signal_type,
            sentinel_run_id=run_id,
        )
        self.tasks[tid] = t
        self.calls.append(("create_task", tid, signal_key))
        return t

    def update_task(self, task_id: str, **fields) -> None:
        self.calls.append(("update_task", task_id, dict(fields)))
        t = self.tasks[task_id]
        mapping = {"sentinelNotifiedAt": "sentinel_notified_at", "sentinelNotifyCount": "sentinel_notify_count",
                   "severity": "severity", "escalatedAt": "escalated_at", "escalationCount": "escalation_count",
                   "sentinelOutcomeLoggedAt": "sentinel_outcome_logged_at", "done": "done", "deadline": "deadline",
                   "completedAt": "completed_at", "status": "status"}
        for k, v in fields.items():
            if k in mapping:
                setattr(t, mapping[k], v)

    def mark_task_notified(self, task_id: str, at) -> None:
        t = self.tasks[task_id]
        self.update_task(task_id, sentinelNotifiedAt=at, sentinelNotifyCount=t.sentinel_notify_count + 1)

    def escalate_task(self, task_id: str, severity: str, at) -> None:
        t = self.tasks[task_id]
        self.update_task(task_id, severity=severity, escalatedAt=at, escalationCount=t.escalation_count + 1)

    def mark_task_outcome_logged(self, task_id: str, at) -> None:
        self.update_task(task_id, sentinelOutcomeLoggedAt=at)

    def close_task(self, task_id: str, at) -> None:
        """Test helper: what a worker does in the app."""
        self.update_task(task_id, done=True, status=S.TASK_STATUS_DONE, completedAt=at)

    def find_recent_sentinel_task(self, terem_id: str, signal_type: str, within_hours: int) -> Task | None:
        from datetime import timedelta
        cutoff = self.clock.now() - timedelta(hours=within_hours)
        for t in self.get_sentinel_tasks(include_done=True):
            if t.terem_id == terem_id and t.signal_type == signal_type:
                if not t.done or (t.created_at and t.created_at >= cutoff):
                    return t
        return None

    # --------------------------------------------------------------- people
    def get_user(self, uid: str) -> User | None:
        return self.users.get(uid)

    def get_farm_manager(self) -> tuple[User | None, str | None]:
        self.calls.append(("get_farm_manager",))
        ordered = sorted(self.perms.items(), key=lambda kv: (kv[1][1] is None, kv[1][1] or self.clock.now()))
        for role in (S.ROLE_ADMIN, S.ROLE_SUPERADMIN):
            for uid, (r, _) in ordered:
                if r == role and uid in self.users:
                    return self.users[uid], (None if role == S.ROLE_ADMIN else "no farm admin found; assigned to a superadmin")
        for uid, _ in ordered:
            u = self.users.get(uid)
            if u and u.role in S.FARM_MANAGER_ROLES:
                return u, "no farm-level admin; fell back to a global admin who has access to the farm"
        return None, "no responsible person found on the farm — task left unassigned"


# --------------------------------------------------------------------------
# A notification transport that records instead of sending
# --------------------------------------------------------------------------
class FakeNotifier:
    """Same contract as sentinel.actions.FcmNotifier.send: (status, reason), never raises."""

    def __init__(self, fail_for: tuple[str, ...] = ()) -> None:
        self.sent: list[tuple[str, str, str, dict]] = []   # (uid, title, body, data)
        self.fail_for = set(fail_for)

    def send(self, user: User, title: str, body: str, data: dict) -> tuple[str, str | None]:
        name = user.display_name or user.uid
        if not user.fcm_token:
            return "skipped", f"no device token registered for {name}"
        if user.uid in self.fail_for:
            return "failed", "FirebaseError: registration token is not valid"
        self.sent.append((user.uid, title, body, dict(data)))
        return "sent", None
