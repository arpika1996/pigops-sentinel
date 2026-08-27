# Devpost submission — text (copy into the form)

**Category:** The Taskmaster
**Hosted project URL:** https://sentinel-console-5b6y6vof6a-ew.a.run.app (live, read-only console)
**Repository:** https://github.com/arpika1996/pigops-sentinel (public; spin-up instructions in README.md)
**Architecture diagram:** `docs/architecture.png` in the repo (upload the same file to Devpost)
**Video:** https://vimeo.com/1218536394 (3:53, public)

**Pre-existing code disclosure:** The PigOps platform (Flutter/Firebase, in production since 2025) and its Firestore data model pre-date the hackathon; `sentinel/schema.py` mirrors that existing model field-for-field. Everything else in the repository — the entire `sentinel/` package, the seed, the agent, both Cloud Run services and the infra scripts — was written during the submission period.

---

## Inspiration

PigOps is my product: a farm-management platform I built for the pig farms I work with every day. Workers record deaths, valve counts, tasks and their progress — every number is in there. But nobody looks at it unless they open the app, and the sentence I kept hearing from the managers was always the same one: *I found out too late.* A mortality spike surfaces at the end of the day; an overdue repair when the animals are already suffering; a room nobody has walked into for days. The platform had all the data and no *initiative*. PigOps Sentinel is the initiative: an agent that watches the farm on its own schedule, decides what matters, and acts — without anyone asking it to. This is not a problem I went looking for; it is the one that was already on my desk.

## What it does

Every 15 minutes (Cloud Scheduler → Cloud Run) the Sentinel runs one loop over the farm's Firestore data:

1. **Scan** — deterministic rules find candidates: a mortality spike, a task overdue or about to slip while untouched, a populated room that has gone silent, a valve being flipped back and forth. No model call; most runs find nothing and cost nothing.
2. **Investigate** — for each candidate a Google ADK agent (Gemini 3.5 Flash on Vertex AI) gathers context the way a farm manager would, choosing its own look-ups: room state, 14-day mortality history, whether a treatment is already underway, whether the barn is affected, the valve log. Tools take and return names, never IDs.
3. **Decide** — NOISE / WATCH / ACT with a severity and an English justification that cites the numbers it actually retrieved.
4. **Act** — creates a real PigOps task for the farm's responsible person and notifies them (Firebase Cloud Messaging); for a slipping deadline it notifies the assignee and the manager instead of duplicating the task. Guardrails live in code: dedup window, per-run cap, explained assignee fallback.
5. **Follow up** — every run re-checks the tasks it created itself: still open past 24 h → severity up, manager nudged, an escalation logged; closed → the outcome logged.

Every outcome — including "this room is fine" — is one document in the decision log, and the **console** renders it live: each card is *Observed / Reasoned / Acted*, with the model's full tool trace ("why I looked → what I found") one click away. That log is the product: it is what proves the agent decided something on its own.

## How we built it

- **Google ADK 2.x** — one `LlmAgent` with six read tools (`FunctionTool`), structured output (a Pydantic `Decision`), one session per run; a `before_tool_callback` enforces a per-candidate tool budget.
- **Gemini 3.5 Flash on Vertex AI** (location `global`) — every model call; the process refuses to start if it finds a Gemini API key, so nothing can leak to the public API.
- **Cloud Run** — two services from one image: the private `sentinel-agent` (`POST /run`, OIDC only, concurrency 1) and the public `sentinel-console` (FastAPI; the page polls — 30 s idle, 2 s during a run, nothing while the tab is hidden — so watching the agent never holds an instance open).
- **Firestore** — the PigOps data model, field-for-field, plus the agent's own `sentinelLog` and `sentinelRuns` collections; **Cloud Scheduler** ticks it; **Firebase Cloud Messaging** for the pushes.
- Python 3.13, FastAPI, pydantic-settings; 107 tests that need neither the model nor Firestore (a pure seed builder and a faked repository); `infra/setup_gcp.sh` bootstraps the whole GCP project, `infra/deploy.sh` builds and deploys both services and the scheduler in one go.

## Architecture decisions

- **The model never writes.** The agent gets six read-only tools; every write — task, notification, escalation, outcome — is deterministic code the model cannot argue with (dedup window, per-run cap, notification stamps, assignee fallback). What comes back from the LLM is a typed `Decision`, not an action. That line is what makes an autonomous writer safe to leave running.
- **State lives in Firestore, never in the process.** A run is a `sentinelRuns` document (phase, counters, status); a verdict is a `sentinelLog` document; progress is stamped on the tasks themselves (`sentinelNotifiedAt`, `escalatedAt`, `sentinelOutcomeLoggedAt`). Nothing is held in memory between ticks, so the service scales to zero, survives being killed mid-run, and an overlap guard refuses a second concurrent run.
- **Least privilege, end to end.** Separate service accounts (agent: `aiplatform.user` + `datastore.user`; console: `datastore.viewer` only), the agent service is private — an OIDC call from Cloud Scheduler is the only way in — and the process refuses to boot if it finds a Gemini API key, so model calls can only reach Vertex AI. Tools take and return names, never raw IDs.
- **Failure is a logged outcome, not silence.** A failed investigation, a failed action and a failed run each leave their own entry (`failed`, `run_failed`, `partial`); one bad candidate never takes the run down with it. All of it is covered by 107 tests that need neither the model nor Firestore.

## Data

A synthetic demo farm generated by the repository's own seed script (`python -m sentinel.seed`): the app's demo farm layout (2 barns, 12 rooms, 192 valves), 21 days of mortality and valve-log history relative to *now*, six planted anomalies and three decoys, three fictional people. No real farm data, no real names, no service-account keys anywhere — Application Default Credentials and least-privilege service accounts only.

## Challenges

- **Judgement, not rules.** Two rooms both cross the mortality threshold; one is a genuine spike, the other has a known problem with a treatment already underway. Getting the agent to say *ACT* for the first and *NOISE* for the second — and to explain both — is what the whole project stands or falls on. It came down to giving the model the manager's questions (is it unusual for *this* room, is someone already on it, is it spreading) instead of the answers.
- **Autonomy that does not spam.** A 15-minute cadence over the same farm state would create the same task, send the same notification and re-decide the same NOISE every quarter hour. Idempotency became a first-class design: open Sentinel tasks, recently closed ones, notified deadlines and identical-evidence verdicts are all "handled" before the model is even called; overlapping runs refuse to start.
- **Trusting the log.** The model's own account of what it looked up is merged onto the *real* tool trace; a claimed look-up that never ran stays in the log flagged `verified: false`. Failures are logged as failures — a broken investigation or run leaves an entry, never silence.
- **Small things that bite:** Gemini 3.5 is only served from the `global` Vertex location; the SDK's Vertex flag changed names between versions (we set both); Google's front end answers `/healthz` on `*.run.app` itself.

## Accomplishments

An agent that has been running unattended in the cloud, making and logging its own decisions — and a console where you can watch it think, including the times it decides not to act.

## What we learned

The interesting part of an autonomous agent is not the model call — it is everything around it: what it is allowed to do, how it avoids doing it twice, how it explains itself, and how it fails. Most of the code is that. And a decision log that includes the "nothing to do here" verdicts is worth more than an alarm feed: it shows judgement.

## What's next

Point it at a real farm (the data model is already the production one), let the follow-up outcomes feed back into the decision (which kinds of tasks get closed fast, which get ignored), Hungarian task text for the workers (`TASK_LANGUAGE=hu`), and a weekly narrated digest for the manager.

## Built with

Python · Google ADK · Gemini 3.5 Flash · Vertex AI · Cloud Run · Firestore · Cloud Scheduler · Firebase Cloud Messaging · FastAPI · Cloud Build · Artifact Registry

---

## Judge-facing testing instructions (Devpost "Testing instructions" field — HARD LIMIT ~255 characters, longer text is silently dropped)

Just open the console URL: public, read-only, live - the agent ticks every 15 min, so cards arrive on their own. Card = Observed / Reasoned (expand for the tool trace) / Acted; grey = NOISE, it chose not to act. Demo data. Local repro: README, 5 min.
