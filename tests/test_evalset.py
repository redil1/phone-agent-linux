"""Replay the versioned call scenarios against the real policy layer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from phone_agent_gateway.ai_bridge.agent_policy import AgentPolicyRuntime
from phone_agent_gateway.ai_bridge.memory.memory_manager import LayeredMemoryManager
from phone_agent_gateway.ai_bridge.tasks.evalset import load_evalset, run_scenario

EVALSET = (
    Path(__file__).resolve().parents[1]
    / "ai_bridge"
    / "tasks"
    / "evalsets"
    / "iptv_subscription_sales.evalset.json"
)


def _runtime(scenario: Any, tmp_path: Path) -> AgentPolicyRuntime:
    return AgentPolicyRuntime(
        caller_id="anonymous",
        task_id=scenario.task_id,
        language=scenario.language,
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
    )


def test_the_evalset_loads() -> None:
    scenarios = load_evalset(EVALSET)
    assert len(scenarios) >= 6
    assert all(scenario.turns for scenario in scenarios)


@pytest.mark.parametrize("scenario", load_evalset(EVALSET), ids=lambda s: s.name)
def test_scenario_passes(scenario: Any, tmp_path: Path) -> None:
    runtime = _runtime(scenario, tmp_path)
    # A question is open after the greeting in every one of these scenarios.
    runtime._question_open = True
    result = run_scenario(scenario, runtime)
    assert result.passed, "\n".join(result.failures)
