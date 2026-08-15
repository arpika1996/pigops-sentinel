"""Shared test fixtures.

Tests never touch a real Firestore: the seed builder is pure, and the scanner
is exercised through an in-memory repository fake built from the seed plan.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

# Make sure no stray API key trips the Vertex-only guard in sentinel.config.
os.environ.pop("GOOGLE_API_KEY", None)
os.environ.pop("GEMINI_API_KEY", None)

from sentinel.clock import Clock  # noqa: E402
from sentinel.seed.demo_data import FARM_ID_DEFAULT, build_demo_plan  # noqa: E402

FROZEN_LOCAL = datetime(2026, 8, 15, 10, 30)  # a Saturday morning, farm-local


@pytest.fixture(scope="session")
def frozen_clock() -> Clock:
    c = Clock()
    c.freeze(FROZEN_LOCAL)
    return c


@pytest.fixture(scope="session")
def demo_plan(frozen_clock: Clock):
    return build_demo_plan(FARM_ID_DEFAULT, frozen_clock, seed=42)
