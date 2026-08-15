"""Decision schema guardrails and the tolerant output parser."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sentinel.agent.investigator import _parse_decision
from sentinel.agent.prompts import build_instruction
from sentinel.agent.schemas import Decision
from sentinel.config import Settings


def test_act_requires_severity_and_title():
    with pytest.raises(ValidationError):
        Decision(decision="ACT", reasoning="x")
    with pytest.raises(ValidationError):
        Decision(decision="ACT", severity="high", reasoning="x")
    d = Decision(decision="ACT", severity="high", reasoning="x", action_title="Check the room")
    assert d.severity == "high"


def test_non_act_normalises_severity_to_none():
    d = Decision(decision="WATCH", severity="low", reasoning="x", watch_reason="if it repeats")
    assert d.severity is None
    d = Decision(decision="NOISE", reasoning="x")
    assert d.severity is None and d.action_title is None


def test_parse_decision_plain_and_fenced():
    raw = '{"decision":"NOISE","reasoning":"fine","context_gathered":[]}'
    d, err = _parse_decision(raw)
    assert d and d.decision == "NOISE" and err is None
    fenced = "```json\n" + raw + "\n```"
    d, err = _parse_decision(fenced)
    assert d and err is None
    d, err = _parse_decision("")
    assert d is None and "no final response" in err
    d, err = _parse_decision('{"decision":"ACT","reasoning":"x"}')
    assert d is None and "did not validate" in err


def test_instruction_states_the_hard_rules():
    text = build_instruction(Settings(_env_file=None))
    for must in ("English", "by NAME", "cite the numbers", "DEADLINE_RISK", "valve", "NOISE", "WATCH", "ACT"):
        assert must in text
    hu = build_instruction(Settings(task_language="hu", _env_file=None))
    assert "Hungarian" in hu and "Hungarian" not in text.split("Task title/description language")[1].split("\n")[0]
