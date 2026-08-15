"""Central configuration — every tunable lives here and is env-overridable.

Values are read from the process environment (and from a local ``.env`` file
during development). Nothing else in the codebase reads ``os.environ`` for
tuning; import ``settings`` from here instead.

Two guardrails are enforced at import time:

1. Model access must go through **Vertex AI**. If a Gemini API key is present
   in the environment we refuse to start, because google-genai would silently
   prefer it and the spend would land on ``generativelanguage.googleapis.com``
   instead of the Vertex AI line (see SPEC.md §2).
2. The Vertex flags are exported in *both* spellings that the SDK lineage has
   used, so the ADK/google-genai client always resolves to Vertex mode.
"""

from __future__ import annotations

import os
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Field names double as environment variable names."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Google Cloud ------------------------------------------------------
    google_cloud_project: str = Field(
        default="pigops-sentinel",
        description="GCP project that hosts Firestore, Cloud Run and Vertex AI.",
    )
    gcp_region: str = Field(
        default="europe-west1",
        description="Region for Cloud Run / Firestore / Cloud Scheduler.",
    )
    google_cloud_location: str = Field(
        default="global",
        description=(
            "Vertex AI location for Gemini calls. Gemini 3.5 is served from "
            "'global', not from europe-west1."
        ),
    )
    gemini_model: str = Field(default="gemini-3.5-flash")
    firestore_emulator_host: str | None = Field(
        default=None,
        description="Set (e.g. 127.0.0.1:8080) to talk to the local emulator.",
    )

    # ---- Farm scope --------------------------------------------------------
    demo_farm_id: str = Field(
        default="demo-farm",
        description="Firestore document ID under `telepek/` the agent watches.",
    )
    farm_timezone: str = Field(
        default="Europe/Budapest",
        description="Operational day boundary for 'today' calculations.",
    )

    # ---- Phase 1 scanner thresholds ---------------------------------------
    scan_interval_minutes: int = Field(default=15, ge=1)
    mortality_spike_threshold: int = Field(
        default=4,
        ge=1,
        description="Absolute number of deaths in one room on one day that "
        "makes the room a candidate. Set from farm experience; the 14-day "
        "baseline is context, not a trigger.",
    )
    mortality_baseline_days: int = Field(default=14, ge=1)
    valve_change_threshold: int = Field(
        default=25,
        ge=1,
        description="Room-level daily count of valve modifications above "
        "which the room is a VALVE_INSTABILITY candidate.",
    )
    valve_flap_threshold: int = Field(
        default=5,
        ge=2,
        description="Per-valve daily modification count that also qualifies "
        "as VALVE_INSTABILITY (a single valve being changed back and forth). "
        "Mirrors the production `findSzelepCsapkodas` default.",
    )
    silent_room_hours: int = Field(
        default=36,
        ge=1,
        description="A populated room with no data written for this long "
        "becomes a SILENT_ROOM candidate.",
    )
    deadline_soon_hours: int = Field(
        default=24,
        ge=1,
        description="Open tasks due within this window and untouched are "
        "DEADLINE_RISK candidates (overdue tasks always are).",
    )

    # ---- Phase 3/4/5 guardrails -------------------------------------------
    max_tasks_per_run: int = Field(default=5, ge=0)
    dedup_window_hours: int = Field(default=48, ge=1)
    escalation_hours: int = Field(default=24, ge=1)

    # ---- Console -----------------------------------------------------------
    console_port: int = Field(default=8081)

    # ---- Derived helpers ---------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.farm_timezone)

    @property
    def uses_emulator(self) -> bool:
        return bool(self.firestore_emulator_host)

    @model_validator(mode="after")
    def _guard_vertex_only(self) -> "Settings":
        """Refuse to run if a Gemini API key could hijack model routing."""
        for var in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
            if os.environ.get(var):
                raise RuntimeError(
                    f"{var} is set. PigOps Sentinel must call Gemini through "
                    "Vertex AI only — unset the API key (see SPEC.md §2)."
                )
        # Force Vertex mode in both spellings the SDK lineage understands.
        os.environ.setdefault("GOOGLE_GENAI_USE_ENTERPRISE", "TRUE")
        os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", self.google_cloud_project)
        os.environ.setdefault("GOOGLE_CLOUD_LOCATION", self.google_cloud_location)
        if self.firestore_emulator_host:
            os.environ.setdefault("FIRESTORE_EMULATOR_HOST", self.firestore_emulator_host)
        return self


Decision = Literal["NOISE", "WATCH", "ACT"]
Severity = Literal["low", "medium", "high"]
SignalType = Literal["MORTALITY_SPIKE", "DEADLINE_RISK", "SILENT_ROOM", "VALVE_INSTABILITY"]

settings = Settings()
