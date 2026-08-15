# Demo video — shot list and narration (~4 minutes, English)

Target: 3:45–4:15. One continuous screen recording, voice-over in English. Two windows side by side:
**left** the console in a browser (https://sentinel-console-5b6y6vof6a-ew.a.run.app), **right** a terminal.
Browser zoom 110–125 % so the cards are readable at 1080p.

## Pre-flight (10 minutes before recording)

The scheduler is normally **paused** (`gcloud scheduler jobs describe sentinel-tick --location europe-west1` → `PAUSED`).
⚠️ A manual `gcloud scheduler jobs run …` is **refused while the job is paused**
(`FAILED_PRECONDITION: Job.state must be ENABLED for RunJob`), so enable it for the take and pause it again afterwards:

```bash
gcloud scheduler jobs resume sentinel-tick --location europe-west1   # before recording
gcloud scheduler jobs pause  sentinel-tick --location europe-west1   # after recording
```

While it is enabled a scheduled tick also fires at :00/:15/:30/:45 — start the take **just after** a quarter hour so
an automatic run cannot collide with the one you trigger (an overlapping run is refused as `skipped`).

Open three Google Cloud Console tabs (Cloud Run services, sentinel-agent logs, Cloud Scheduler) — the rules require
showing the backend on Google Cloud in the video.

```bash
cd C:\Projects\pigops-sentinel && .venv\Scripts\activate

# 1. clean slate: fresh demo farm, empty log (the console notices the reset by itself and re-renders empty)
python -m sentinel.seed --reset --reset-log

# 2. sanity: 6 candidates, 0 decoys
python -m sentinel.scanner

# 3. open the console in the browser, hard-reload — the feed says "Waiting for the first decision…"
#    and the header shows "no run yet"
```

Keep this terminal for the recording; have `gcloud scheduler jobs run sentinel-tick --location europe-west1` typed and ready.

If something goes wrong mid-take: `python -m sentinel.seed --reset --reset-log`, reload the console, start again — the seed is deterministic, every take is identical.

## Shots

**0:00 – 0:25 · The problem (console, empty)**
> "This is PigOps — a farm-management platform running on real pig farms in Hungary. Workers record deaths, valve counts, tasks. What the platform never had is *initiative*: nobody looks at the data unless they open the app. PigOps Sentinel is an autonomous agent that does the looking — on its own schedule, every fifteen minutes, without anyone asking."

**0:25 – 0:50 · The stack (architecture diagram, `docs/architecture.png`, ~15 s, then back to the console)**
> "It's a Google ADK agent running Gemini 3.5 Flash on Vertex AI, deployed on Cloud Run, woken by Cloud Scheduler, reading and writing the farm's Firestore. One run has five phases: a deterministic scan for candidates, then the agent investigates each one with tools — choosing its own look-ups — decides NOISE, WATCH or ACT, acts through guarded tools, and follows up on its own tasks. Everything it does lands in one decision log, which is what you see on this console."

**0:50 – 1:05 · Trigger (terminal)**
> "The demo farm has six planted anomalies today and three decoys. I'll ask Cloud Scheduler to fire the tick right now instead of waiting for the next quarter hour."

```bash
gcloud scheduler jobs run sentinel-tick --location europe-west1
```

**1:05 – 1:35 · Live (console header)** — the pill turns green: *scanning the farm… → investigating DEADLINE_RISK — A-Telep / 2.Terem*
> "The scanner found the six candidates in a second and none of the decoys — that part is rules, no tokens spent. Now the agent takes them one at a time. Watch the header: that's the run's live phase, straight from Firestore."

While the first cards arrive (deadline risks come first, alphabetically):
> "First card: a fan-repair task two days overdue, never touched. The agent looked at the room, saw three deaths today against a near-zero baseline, and decided ACT, high — but it did *not* create a duplicate task; it notified the assignee and the farm manager. On the demo farm nobody has a device token, so the log says exactly that: skipped, and why. Failing loudly is a rule here."

**1:35 – 2:20 · Judgement (the two mortality cards)**
Open *Reasoned* on **B-Telep / 5.Terem** (ACT, high):
> "Five respiratory deaths in a room that averaged under one a day. Here's the tool trace — every look-up the agent made, with its own 'why I looked' and 'what I found'. It checked the room, the 14-day history, whether a treatment was already underway, whether the barn is affected. Verdict: ACT, high, and a task is created for Fenyvesi Márton, the farm manager, due within four hours."

Then **A-Telep / 3.Terem** (NOISE):
> "Same signal type — four deaths today, over the threshold. But this room has had a known respiratory problem for ten days and there's an open treatment task with daily work-log entries. The agent found that and said NOISE: 'no action needed, already handled'. That's the difference between an alarm and judgement — and it's logged just the same."

**2:20 – 2:50 · The rest (scroll)**
> "A room with 189 pigs and no data for three days while its neighbours are active — a task to go and look. A valve flipped plus-two-minus-two nine times, net plus one — data-entry confusion, low severity, 'verify the count'. And the drinker check due in six hours, untouched — a medium notification."

**2:50 – 3:20 · Idempotent, cheap, safe (terminal + console)**
Wait until the header pill is grey again (*idle · last run … · 6 decided · 3 tasks*) — the first run takes
2.5–3 minutes, so this is where you **jump-cut** in the edit if the cards were slow. Only then fire the tick again
(firing while it still runs is refused: 409 busy / a `skipped` run — correct, but not what you want on camera):
```bash
gcloud scheduler jobs run sentinel-tick --location europe-west1
```
It is over in ~10 seconds — no model call at all.
> "Now the same tick again — this is what most of the day looks like. Every candidate is 'already handled': open Sentinel tasks cover the rooms, the deadlines were notified, the NOISE verdict cools down on identical evidence. Nothing reaches the model, zero tokens, no duplicate task, no nagging. The follow-up phase still runs — its own tasks are re-checked every run; past twenty-four hours it escalates: severity up, the manager nudged, an escalation entry in the log; when a task gets closed, the outcome is logged too."

(The header shows *idle · last run … · 0 decided · 0 tasks*; the stats show 6 already handled.)

**3:20 – 3:50 · It runs on Google Cloud (REQUIRED by the rules — show the Console) + guardrails**
Switch to a browser tab with the Google Cloud Console, project `pigops-sentinel`, and show in ~20 s:
1. **Cloud Run → Services**: `sentinel-agent` and `sentinel-console` with their `…run.app` URLs and the green ticks
   (https://console.cloud.google.com/run?project=pigops-sentinel)
2. **Cloud Run → sentinel-agent → Logs**: the `POST /run?trigger=scheduler … 200` and `run … ok: N candidate(s)` lines from the run you just watched
3. **Cloud Scheduler**: the `sentinel-tick` job, `*/15 * * * *` (https://console.cloud.google.com/cloudscheduler?project=pigops-sentinel)
4. optionally **Vertex AI → Dashboard / Model Garden → Gemini 3.5 Flash** request count (https://console.cloud.google.com/vertex-ai?project=pigops-sentinel)
> "All of this runs on Google Cloud: two Cloud Run services — the private agent that Cloud Scheduler wakes, and the public console — Firestore underneath, and every model call on Vertex AI, Gemini 3.5 Flash. The tasks are real PigOps tasks — the app shows them, workers close them, and the Sentinel writes down what came of it. Guardrails are code, not prompt: dedup window, five new tasks per run at most, an explained fallback when nobody is assignable, and the process refuses to start if it finds a Gemini API key — Vertex only."

(Have these three Console tabs open and logged in BEFORE recording; keep the browser zoom readable.)

**3:50 – 4:05 · Close**
> "PigOps Sentinel: an agent that watches a farm on its own, decides with judgement, acts through the tools it's allowed to use, follows up when ignored — and shows its reasoning for every single decision, including the ones where it decided everything is fine. Built with Google ADK, Gemini 3.5 Flash on Vertex AI, Cloud Run, Firestore and Cloud Scheduler. Thank you."

## Notes

- Timing: the first full run takes ~2.5–3 minutes on Gemini 3.5 Flash and the cards arrive one by one in
  scan order (the two deadline risks first, then the two mortality rooms, the silent room, the valve). Talk over
  it; plan **one jump cut** in the edit between "the rest" and the second tick if needed. Alternatively, run the
  first tick 4 minutes *before* recording and start the take on a full console — you lose the live "investigating…"
  header but the whole thing fits in real time.
- If the model's wording differs from the script (it will, slightly), read what is on the card — the numbers are the same every take.
- After recording, decide whether to `gcloud scheduler jobs resume sentinel-tick --location europe-west1` for the judging window (a live console with runs today; a few cents a day) or leave it paused — the console keeps showing the full log either way.
- Devpost: the diagram is `docs/architecture.png` (3:2, 0.6 MB); the console screenshot is `docs/console.png`.
