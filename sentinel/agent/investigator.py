"""Phase 2 + 3: the ADK agent investigates a candidate and decides.

One :class:`Investigator` per run. It owns the ADK ``LlmAgent`` (Gemini on
Vertex AI), an in-memory ``Runner`` and **one session for the whole run** —
each candidate is one user turn in that session (SPEC §4: "the whole run is
one ADK session"), so the model keeps the farm context between candidates.

What comes back for every candidate is an :class:`InvestigationResult`:

* ``decision``    the validated :class:`Decision` (or ``None`` on failure)
* ``tool_trace``  what the agent *actually* called — from the toolbox, not
                  from the model's own account of itself
* latency, token usage, model name, raw output, error (if any)

Nothing here writes to Firestore; that is Phase 4's job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Callable

from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig
from google.adk.runners import InMemoryRunner
from google.genai import types

from ..config import Settings, settings as default_settings
from ..scanner import Candidate, FarmSnapshot
from .prompts import build_instruction
from .schemas import Decision
from .tools import ToolBox, ToolCall

log = logging.getLogger(__name__)

APP_NAME = "pigops-sentinel"
USER_ID = "sentinel"

# Called with (phase, detail) — the console's live status feed hooks in here.
StatusCallback = Callable[[str, str], None]


@dataclass
class InvestigationResult:
    candidate: Candidate
    decision: Decision | None
    raw_output: str
    tool_trace: list[ToolCall]
    latency_ms: int
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0
    model: str = ""
    error: str | None = None
    started_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.decision is not None and self.error is None

    def trace_as_log(self) -> list[dict[str, Any]]:
        return [t.as_log() for t in self.tool_trace]


class Investigator:
    """Builds the agent once per run and investigates candidates one by one."""

    def __init__(
        self,
        repo: Any,
        snapshot: FarmSnapshot,
        run_id: str,
        cfg: Settings | None = None,
        status: StatusCallback | None = None,
    ) -> None:
        self.cfg = cfg or default_settings
        self.run_id = run_id
        self.snapshot = snapshot
        self.status = status or (lambda phase, detail: None)
        self.toolbox = ToolBox(repo, snapshot, self.cfg)
        self._tool_calls_this_turn = 0

        self.agent = LlmAgent(
            name="pigops_sentinel",
            description="Investigates farm anomalies and decides whether a person is needed today.",
            model=self.cfg.gemini_model,
            instruction=build_instruction(self.cfg),
            tools=self.toolbox.tools(),
            output_schema=Decision,
            # One session per run keeps the trace together, but each candidate is
            # investigated on its own evidence: no history from earlier candidates
            # (bounded tokens; the model re-queries what it needs anyway).
            include_contents="none",
            generate_content_config=types.GenerateContentConfig(temperature=self.cfg.model_temperature),
            before_tool_callback=self._before_tool,
        )
        self.runner = InMemoryRunner(agent=self.agent, app_name=APP_NAME)
        self._session_ready = False

    # ------------------------------------------------------------ callbacks
    def _before_tool(self, tool, args: dict[str, Any], tool_context) -> dict[str, Any] | None:
        """Per-candidate tool budget (a runaway loop must not run up the bill)."""
        self._tool_calls_this_turn += 1
        room = args.get("room") or ""
        self.status("investigating", f"{tool.name}({room})" if room else f"{tool.name}()")
        if self._tool_calls_this_turn > self.cfg.max_tool_calls_per_candidate:
            return {"error": f"Tool budget of {self.cfg.max_tool_calls_per_candidate} calls exhausted — decide with what you have."}
        return None

    # -------------------------------------------------------------- session
    async def _ensure_session(self) -> None:
        if not self._session_ready:
            await self.runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=self.run_id)
            self._session_ready = True

    # -------------------------------------------------------------- prompts
    def _candidate_message(self, c: Candidate) -> str:
        now_local = self.snapshot.now.astimezone(self.snapshot.tz)
        lines = [
            f"Farm: {self.snapshot.structure.farm.name}",
            f"Local time now: {now_local:%A, %Y-%m-%d %H:%M}",
            f"Signal: {c.signal_type}",
            f"Room: {c.room_name}",
            f"Observation: {c.observation}",
            f"Evidence: {json.dumps(c.evidence, ensure_ascii=False, default=str)}",
            "",
            "Investigate this candidate and decide.",
        ]
        return "\n".join(lines)

    # ---------------------------------------------------------- investigate
    async def investigate(self, candidate: Candidate) -> InvestigationResult:
        await self._ensure_session()
        self.toolbox.trace = []
        self._tool_calls_this_turn = 0
        started = datetime.now().astimezone()
        t0 = time.perf_counter()
        self.status("investigating", candidate.room_name)

        prompt_tokens = output_tokens = total_tokens = llm_calls = 0
        final_text = ""
        error: str | None = None
        try:
            msg = types.Content(role="user", parts=[types.Part(text=self._candidate_message(candidate))])
            async for event in self._events(msg):
                if event.usage_metadata:
                    um = event.usage_metadata
                    prompt_tokens += um.prompt_token_count or 0
                    output_tokens += um.candidates_token_count or 0
                    total_tokens += um.total_token_count or 0
                    llm_calls += 1
                if event.is_final_response() and event.content and event.content.parts:
                    final_text = "".join(p.text or "" for p in event.content.parts if p.text)
        except Exception as exc:  # noqa: BLE001 — must reach the log, never vanish
            error = f"{type(exc).__name__}: {exc}"
            log.exception("investigation failed for %s", candidate.key)

        decision: Decision | None = None
        if error is None:
            decision, parse_error = _parse_decision(final_text)
            if decision is None:
                error = parse_error
        self.status("decided", f"{candidate.room_name}: {decision.decision if decision else 'FAILED'}")

        # trace copy: the toolbox list is reset on the next candidate
        return InvestigationResult(
            candidate=candidate,
            decision=decision,
            raw_output=final_text,
            tool_trace=list(self.toolbox.trace),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            llm_calls=llm_calls,
            model=self.cfg.gemini_model,
            error=error,
            started_at=started,
        )

    async def _events(self, msg: types.Content) -> AsyncIterator[Any]:
        run_config = RunConfig(max_llm_calls=self.cfg.max_tool_calls_per_candidate + 4)
        async for event in self.runner.run_async(user_id=USER_ID, session_id=self.run_id, new_message=msg, run_config=run_config):
            yield event

    async def investigate_all(self, candidates: list[Candidate]) -> list[InvestigationResult]:
        results = []
        for c in candidates:
            results.append(await self.investigate(c))
        return results

    async def close(self) -> None:
        try:
            await self.runner.close()
        except Exception:  # pragma: no cover
            pass


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def _parse_decision(text: str) -> tuple[Decision | None, str | None]:
    if not text.strip():
        return None, "model returned no final response"
    try:
        return Decision.model_validate_json(text), None
    except Exception as first:  # noqa: BLE001
        m = _JSON_BLOCK.search(text)
        if m:
            try:
                return Decision.model_validate_json(m.group(0)), None
            except Exception as second:  # noqa: BLE001
                return None, f"decision did not validate: {second}"
        return None, f"decision did not validate: {first}"


# --------------------------------------------------------------------------
# Sync convenience for scripts
# --------------------------------------------------------------------------
def investigate_candidates(
    repo: Any,
    snapshot: FarmSnapshot,
    candidates: list[Candidate],
    run_id: str,
    cfg: Settings | None = None,
    status: StatusCallback | None = None,
) -> list[InvestigationResult]:
    """Run the whole investigation on a fresh event loop (one session for the run)."""

    async def _main() -> list[InvestigationResult]:
        inv = Investigator(repo, snapshot, run_id, cfg, status)
        try:
            return await inv.investigate_all(candidates)
        finally:
            await inv.close()

    return asyncio.run(_main())
