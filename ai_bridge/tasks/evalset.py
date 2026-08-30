"""Replay a scripted call and assert how the agent handled it.

Borrowed from ADK's ``.evalset.json`` idea: a versioned file of caller turns
with expectations, replayed against the real policy layer. It calls no model
and places no call, so it runs in the ordinary test suite and in CI.

What it can check is the deterministic half - which turns were treated as
answerable, which produced a repair, which slots filled, what stage was
reached, and the recorded outcome. Whether the agent *sounded* human is still
a judgement only a real call can make.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..conversation_repair import TurnQuality
from .call_state import TaskRuntime


@dataclass(frozen=True, slots=True)
class TurnExpectation:
    """One caller turn and what should happen to it."""

    caller: str
    quality: str = ""
    fills: tuple[str, ...] = ()
    agent_says: str = ""

    @classmethod
    def parse(cls, entry: Any) -> TurnExpectation:
        if not isinstance(entry, dict):
            raise ValueError("each turn must be an object")
        caller = str(entry.get("caller", "")).strip()
        if not caller:
            raise ValueError("each turn needs caller text")
        return cls(
            caller=caller,
            quality=str(entry.get("quality", "")).strip(),
            fills=tuple(str(item) for item in entry.get("fills", []) or []),
            agent_says=str(entry.get("agent_says", "")).strip(),
        )


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    task_id: str
    language: str
    turns: tuple[TurnExpectation, ...]
    expect_stage: str = ""
    expect_outcome: str = ""
    expect_collected: tuple[str, ...] = ()

    @classmethod
    def parse(cls, entry: Any) -> Scenario:
        if not isinstance(entry, dict):
            raise ValueError("each scenario must be an object")
        name = str(entry.get("name", "")).strip()
        if not name:
            raise ValueError("each scenario needs a name")
        return cls(
            name=name,
            task_id=str(entry.get("task_id", "")).strip(),
            language=str(entry.get("language", "en-US")).strip(),
            turns=tuple(TurnExpectation.parse(turn) for turn in entry.get("turns", []) or []),
            expect_stage=str(entry.get("expect_stage", "")).strip().upper(),
            expect_outcome=str(entry.get("expect_outcome", "")).strip(),
            expect_collected=tuple(str(item) for item in entry.get("expect_collected", []) or []),
        )


@dataclass
class ScenarioResult:
    name: str
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def load_evalset(path: Path) -> tuple[Scenario, ...]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    scenarios = data.get("scenarios", data) if isinstance(data, dict) else data
    if not isinstance(scenarios, list):
        raise ValueError("an evalset must contain a list of scenarios")
    return tuple(Scenario.parse(entry) for entry in scenarios)


def run_scenario(scenario: Scenario, runtime: Any) -> ScenarioResult:
    """Replay one scenario against a live AgentPolicyRuntime.

    ``runtime`` is an ``AgentPolicyRuntime``. It is passed in rather than built
    here so the caller controls memory and persona isolation.
    """

    result = ScenarioResult(name=scenario.name)
    task: TaskRuntime = runtime.task

    for index, turn in enumerate(scenario.turns, start=1):
        where = f"turn {index} ({turn.caller!r})"
        quality = runtime.classify_turn(turn.caller)

        if turn.quality and quality.value != turn.quality:
            result.failures.append(
                f"{where}: expected quality {turn.quality!r}, got {quality.value!r}"
            )

        # Only a turn the agent would actually act on may fill a slot.
        if quality is TurnQuality.ACTIONABLE:
            actions = task.observe_caller_turn(turn.caller)
            filled = set(actions.state_delta)
        else:
            filled = set()

        missing = set(turn.fills) - filled
        if missing:
            result.failures.append(
                f"{where}: expected to fill {sorted(missing)}, filled {sorted(filled)}"
            )

        if turn.agent_says:
            spoken, _stop = runtime.guard_sentence(turn.agent_says, is_first=True)
            if not spoken:
                result.failures.append(f"{where}: the agent's reply was blocked outright")

    if scenario.expect_stage and task.stage != scenario.expect_stage:
        result.failures.append(
            f"expected to end in stage {scenario.expect_stage!r}, ended in {task.stage!r}"
        )
    if scenario.expect_collected:
        missing = set(scenario.expect_collected) - set(task.state)
        if missing:
            result.failures.append(f"expected to have collected {sorted(missing)}")
    if scenario.expect_outcome and task.outcome.value != scenario.expect_outcome:
        result.failures.append(
            f"expected outcome {scenario.expect_outcome!r}, got {task.outcome.value!r}"
        )
    return result
