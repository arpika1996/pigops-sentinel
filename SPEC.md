# PigOps Sentinel — Technical Specification

Hackathon: All Things Agentic Hackathon (Google / Devpost)
Track: **The Taskmaster**
Deadline: 2026-09-01 02:00 CEST

---

## 1. What we are building

An autonomous agent that monitors live pig farm data, decides what
matters, and acts on it — creating tasks, notifying the right person,
and escalating when ignored. No user request triggers it. It runs on
its own schedule.

It sits on top of PigOps (an existing Flutter/Firebase farm management
platform in production on three Hungarian farms). The agent does not
replace the platform; it adds the one thing the platform never had:
initiative.

### Mandatory stack (hackathon rules — all three required)

| Requirement | Our choice |
|---|---|
| Gemini 3.5 or newer | Gemini 3.5 Flash via **Vertex AI** |
| Google agent framework | **Google ADK** (Agent Development Kit) |
| Google Cloud service | **Cloud Run** + **Firestore** + **Cloud Scheduler** |

---

## 2. Repository scope

**This is a standalone public repository.** It must be cloneable and
runnable by a judge who has no access to any private PigOps system.

- Direct Firestore access via Firebase Admin SDK — **not** through the
  existing private MCP server.
- No service account keys committed. `.env` in `.gitignore`.
- All project IDs, farm IDs and collection names come from config.
- Ships with a seed script that populates a demo dataset, so the whole
  thing runs end to end without real farm data.

Real production farm data, worker names and real mortality figures
never leave the private system. The demo farm only.

### Google Cloud project

The whole thing runs in a **dedicated GCP project**, separate from the
existing PigOps and other projects. Reasons: hackathon spend stays
isolated and measurable, the demo credits are not mixed with production
billing, and the entire project can be torn down in one move afterwards
without touching anything that matters.

- One project, created for this build only
- Region: `europe-west1` for Cloud Run, Firestore and Vertex AI —
  keep all three in the same region
- APIs to enable: `aiplatform.googleapis.com`, `run.googleapis.com`,
  `firestore.googleapis.com`, `cloudscheduler.googleapis.com`
- Local dev auth: `gcloud auth application-default login`
- Cloud Run services get their own service account with
  `roles/aiplatform.user` and scoped Firestore access — not the
  default compute account

**Model access must go through Vertex AI, not the Gemini API.** Set
`GOOGLE_GENAI_USE_VERTEXAI=TRUE` along with `GOOGLE_CLOUD_PROJECT` and
`GOOGLE_CLOUD_LOCATION`. Verify against current ADK docs, as these
variable names have changed between versions. After the first real run,
check the billing breakdown to confirm the spend lands on the Vertex AI
line — if calls are reaching `generativelanguage.googleapis.com`
instead, the configuration is wrong and hackathon credits will not
cover it.

---

## 3. Architecture

```
Cloud Scheduler (every 15 min)
        │
        ▼
Cloud Run service ──► ADK Agent (Gemini 3.5 Flash via Vertex AI)
        │                    │
        │                    ├─ tools ──► Firestore (farm data, read)
        │                    │
        │                    └─ tools ──► Firestore (tasks, write)
        │                                 FCM (notification)
        ▼
Firestore: sentinelLog  ◄── Sentinel Console (Cloud Run, read-only)
```

Two Cloud Run services:
1. **sentinel-agent** — the agent itself, invoked by Scheduler
2. **sentinel-console** — read-only web UI showing the decision log

---

## 4. The agent loop

Each run executes these phases. The whole run is one ADK session.

### Phase 1 — Scan (deterministic, no LLM)

Cheap, rule-based sweep to find *candidates*. This is intentionally
dumb — it only asks "is anything unusual enough to look at?"

Candidate signal types:

| Signal | Trigger condition |
|---|---|
| `MORTALITY_SPIKE` | **4 or more mortality events in one room on one day** (absolute threshold, config `MORTALITY_SPIKE_THRESHOLD`, default 4) |
| `DEADLINE_RISK` | Open task past due, or due within 24h and untouched |
| `SILENT_ROOM` | A room with no data written for longer than its normal interval |
| `VALVE_INSTABILITY` | Valve modification count in a room exceeds a daily threshold |

Deterministic on purpose: we do not spend LLM tokens deciding that a
normal day is normal. Most runs find zero candidates and exit.

Note on `MORTALITY_SPIKE`: the trigger is an absolute count, set from
real operational experience on the farms — 4 deaths in a single room in
a single day is unusual enough to warrant a look, regardless of that
room's history. The 14-day baseline is **not** used as a trigger, but
it is still retrieved in Phase 2 as context for the reasoning ("4 today
against a 14-day mean of 0.8" is a much stronger observation than "4
today").

### Phase 2 — Investigate (LLM + tools)

For each candidate, the ADK agent gathers context the way a farm
manager would. Available tools:

- `get_structure(farm_id)` — farm → building → room hierarchy.
  **Always resolve IDs to names.** Never surface raw Firestore IDs.
- `get_mortality_history(room_id, days)` — daily series
- `get_room_stats(room_id)` — current room state
- `get_open_tasks(room_id)` — existing open faults/tasks there
- `get_valve_log(room_id, days)` — valve changes

The agent decides which tools to call. Do not hardcode the sequence —
the reasoning trace is part of what we are demonstrating.

### Phase 3 — Decide (LLM)

The agent classifies the candidate into exactly one of:

- `NOISE` — within normal variation, no action, logged only
- `WATCH` — worth monitoring, logged, no task created
- `ACT` — needs a person today, task created

It must produce a written justification in **English**, referring to
entity names, not IDs. The justification must reference the context it
actually retrieved.

**Guardrails:**
- If `ACT`, severity must be one of `low` / `medium` / `high`
- Never create a duplicate task for a signal already open on the same
  room within the configured dedup window
- Hard cap on tasks created per run (config, default 5) — a bad model
  day must not spam the farm

### Phase 4 — Act (tools)

- `create_task(room_id, title, description, assignee_id, severity)`
- `send_notification(user_id, title, body)`

Assignee resolution: look up the responsible user for that farm from
the farm's permission subcollection. If none found, fall back to the
farm admin and note the fallback in the log.

### Phase 5 — Follow up (deterministic + LLM)

Tasks created by the agent are tagged with its own marker. On each run
it re-checks its own previously created tasks:

- Still open past the escalation window → escalate: raise severity,
  notify the farm admin, log the escalation
- Closed → log the outcome (this is the seed of future learning)

---

## 5. The decision log — `sentinelLog`

Every phase-2..5 outcome writes one document. This collection is the
product: it is what the console renders and what proves autonomy.

```
sentinelLog/{logId}
  runId            string      groups entries from one run
  timestamp        timestamp
  farmName         string      resolved name, not ID
  roomName         string      resolved name, e.g. "H2 / 5. terem"
  signalType       string      MORTALITY_SPIKE | DEADLINE_RISK | ...
  observation      string      EN, what was noticed, with numbers
  contextGathered  array       which tools were called and why
  reasoning        string      EN, the model's actual justification
  decision         string      NOISE | WATCH | ACT
  severity         string?     low | medium | high
  actionTaken      object?     { type, taskId, assigneeName, sentAt }
  escalation       object?     { fromSeverity, toSeverity, reason }
  latencyMs        number
```

Write this even for `NOISE`. A console that only shows alarms looks
like a rule engine; one that shows the agent deciding something is
*fine* shows judgement.

---

## 6. Sentinel Console

One page. Cloud Run. **English UI.** Read-only.

- Reverse-chronological feed of `sentinelLog`, live-updating
- Each entry is a card with three visible bands:
  1. **Observed** — observation text + timestamp + room name
  2. **Reasoned** — collapsed by default, expands to show
     `contextGathered` and `reasoning`
  3. **Acted** — the action taken, or "no action needed" for NOISE
- Colour-code by decision (NOISE muted, WATCH amber, ACT red)
- Escalations visually distinct
- A small header strip: last run time, runs today, tasks created today

No login, no filters, no settings. It has one job: make the agent's
thinking visible in a four-minute video.

---

## 7. Configuration

Everything tunable lives in one config module, env-overridable:

- `SCAN_INTERVAL_MINUTES` (default 15)
- `MORTALITY_SPIKE_THRESHOLD` (default 4 — absolute daily count per room)
- `MORTALITY_BASELINE_DAYS` (default 14 — context only, not a trigger)
- `VALVE_CHANGE_THRESHOLD`
- `SILENT_ROOM_HOURS`
- `ESCALATION_HOURS`
- `MAX_TASKS_PER_RUN` (default 5)
- `DEDUP_WINDOW_HOURS`
- `DEMO_FARM_ID`

---

## 8. Non-negotiables

1. **Never print raw Firestore IDs** anywhere a human reads —
   log, console, task text. Resolve to names first.
2. **All agent output in English.** The underlying farm platform is
   Hungarian; the agent's reasoning is not.
3. **Fail loudly, not silently.** A failed tool call must be written
   to `sentinelLog` as a failed run entry, not swallowed.
4. **Idempotent runs.** Two runs in the same window must not double
   up tasks.
5. **No secrets in the repo.** Ever.

---

## 9. Deliverables checklist

- [ ] `sentinel-agent` deployed to Cloud Run, triggered by Scheduler
- [ ] `sentinel-console` deployed to Cloud Run, public URL
- [ ] Seed script producing a believable demo dataset
- [ ] `README.md` in English with step-by-step spin-up instructions
- [ ] Architecture diagram (3:2, under 5 MB)
- [ ] Demo video, ~4 min, English narration

---

## 10. Build order

0. **Create the repository.** Use the GitHub CLI to create a new
   **public** repo named `pigops-sentinel` under the account
   `arpika1996`, initialise it locally, and commit this spec as
   `SPEC.md` in the first commit. Set up `.gitignore` (Python defaults
   plus `.env`, service account JSON, `__pycache__`, local Firestore
   emulator data) **before** the first commit — never let a key reach
   the history. Add a placeholder `README.md`; it gets written properly
   at step 9.

   Then set up the Google Cloud side: create the dedicated project,
   enable the four APIs listed in §2, set the region to `europe-west1`,
   and confirm `gcloud config list` points at the new project — not at
   an existing one.
1. Firestore data access layer + demo seed script
2. Phase 1 scanner (deterministic, no LLM) — verify it finds the
   planted anomalies in the seed data
3. ADK agent skeleton with the read tools, Gemini on Vertex AI
4. Decision phase + `sentinelLog` writing
5. Action tools (task creation, notification)
6. Follow-up / escalation phase
7. Console
8. Deploy both services, wire up Scheduler
9. README, diagram
10. Record video

Get 1–4 working locally before deploying anything. The console can
read from local Firestore during development.
