"""Structured output of the agent — the contract between Phase 3 and Phase 4."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ContextItem(BaseModel):
    """One piece of context the agent gathered (as claimed by the model)."""

    tool: str = Field(description="Name of the tool that was called.")
    why: str = Field(description="Why this look-up was needed, one short sentence.")
    finding: str = Field(description="What it showed, with the actual numbers.")


class Decision(BaseModel):
    """What the agent concluded about one candidate.

    English only. Refer to rooms, barns and people by name — never by ID.
    """

    decision: Literal["NOISE", "WATCH", "ACT"] = Field(
        description=(
            "NOISE = within normal variation, no action; "
            "WATCH = worth monitoring, no task; "
            "ACT = someone must do something today, a task is created."
        )
    )
    severity: Literal["low", "medium", "high"] | None = Field(
        default=None,
        description="Required when decision is ACT; must be null otherwise.",
    )
    reasoning: str = Field(
        description=(
            "Your justification in English, 2-6 sentences, citing the numbers "
            "you actually retrieved (e.g. '5 today against a 14-day mean of 0.7')."
        )
    )
    context_gathered: list[ContextItem] = Field(
        default_factory=list,
        description="The look-ups you made and what each showed.",
    )
    action_title: str | None = Field(
        default=None,
        description=(
            "ACT only. A short, concrete title addressed to the person on the farm. "
            "For MORTALITY_SPIKE / SILENT_ROOM / VALVE_INSTABILITY this becomes the "
            "title of a new task; for DEADLINE_RISK it becomes the title of the "
            "notification sent about the existing task (no duplicate task is created)."
        ),
    )
    action_body: str | None = Field(
        default=None,
        description="ACT only: what exactly to do and check, 1-4 sentences (task description or notification body).",
    )
    watch_reason: str | None = Field(
        default=None,
        description="WATCH only: what would turn this into ACT (the condition to re-check).",
    )

    @model_validator(mode="after")
    def _consistency(self) -> "Decision":
        if self.decision == "ACT":
            if self.severity is None:
                raise ValueError("severity is required when decision is ACT")
            if not self.action_title:
                raise ValueError("action_title is required when decision is ACT")
        else:
            # A guardrail, not a nag: silently normalise so a model quirk never
            # turns into a crash — the log keeps the model's decision.
            object.__setattr__(self, "severity", None)
        return self
