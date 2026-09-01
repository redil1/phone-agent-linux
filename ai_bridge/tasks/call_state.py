"""Delta-aware task state: what has been learned, and what to do next.

The task contract used to be prose in a prompt. It listed seven things to
discover and seven conversation stages, and nothing tracked either, so the
agent wandered, re-asked answered questions, and never knew when it was done.

This borrows three ideas from Google's ADK without adopting the framework:

* delta-aware state - every observation returns the change it made, so the
  change can be logged, evaluated and replayed rather than mutating silently;
* actions as data - a turn returns a stage transition or a stop reason as a
  value instead of hoping the model narrates it;
* declared slots - what the task needs is data the code can check, not an
  instruction the model may ignore.

Everything is declared in the task YAML and stays editable in the Studio.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

MAX_PATTERN_CHARS = 400


class CallOutcome(StrEnum):
    """How the call ended, recorded rather than guessed at afterwards."""

    IN_PROGRESS = "in_progress"
    QUALIFIED = "qualified"
    NEXT_STEP_AGREED = "next_step_agreed"
    CALLBACK_REQUESTED = "callback_requested"
    REFUSED = "refused"
    NOT_REACHABLE = "not_reachable"
    ABANDONED = "abandoned"


@dataclass(frozen=True, slots=True)
class SlotSpec:
    """One thing the task must learn from the caller."""

    id: str
    question: str = ""
    patterns: tuple[re.Pattern[str], ...] = ()

    @classmethod
    def parse(cls, entry: Any) -> SlotSpec:
        """Accept a bare name or a declared slot with its own detection.

        A plain string keeps older contracts working: the slot exists and is
        shown to the model, it simply cannot be auto-detected.
        """

        if isinstance(entry, str):
            return cls(id=entry.strip())
        if not isinstance(entry, dict):
            raise ValueError("each required input must be a name or an object")
        slot_id = str(entry.get("id", "")).strip()
        if not slot_id:
            raise ValueError("a required input needs an id")
        compiled: list[re.Pattern[str]] = []
        for raw in entry.get("detect", []) or []:
            text = str(raw).strip()
            if not text:
                continue
            if len(text) > MAX_PATTERN_CHARS:
                raise ValueError(f"detect pattern for {slot_id!r} is too long")
            try:
                compiled.append(re.compile(text, re.IGNORECASE))
            except re.error as exc:
                raise ValueError(f"detect pattern for {slot_id!r} is invalid: {exc}") from exc
        return cls(
            id=slot_id,
            question=str(entry.get("question", "")).strip(),
            patterns=tuple(compiled),
        )

    def match(self, text: str) -> str | None:
        """Return the caller's own words that filled this slot, if any."""

        for pattern in self.patterns:
            found = pattern.search(text)
            if found:
                return (found.group(0) or "").strip() or None
        return None


@dataclass(frozen=True, slots=True)
class StageSpec:
    """One stage of the conversation, parsed from the contract."""

    name: str
    instruction: str
    requires: tuple[str, ...] = ()

    @classmethod
    def parse(cls, entry: Any) -> StageSpec | None:
        """Parse ``NAME: what to do here`` from the contract's strategy list."""

        if isinstance(entry, dict):
            name = str(entry.get("name", "")).strip().upper()
            if not name:
                return None
            return cls(
                name=name,
                instruction=str(entry.get("instruction", "")).strip(),
                requires=tuple(str(item).strip() for item in entry.get("requires", []) or []),
            )
        text = str(entry).strip()
        match = re.match(r"^([A-Z][A-Z _-]{1,30}):\s*(.+)$", text)
        if not match:
            return None
        return cls(name=match.group(1).strip().upper(), instruction=match.group(2).strip())


@dataclass(frozen=True, slots=True)
class TurnActions:
    """What a turn changed, returned as data rather than narrated."""

    state_delta: dict[str, Any] = field(default_factory=dict)
    stage_from: str = ""
    stage_to: str = ""
    outcome: CallOutcome | None = None

    @property
    def changed(self) -> bool:
        return bool(self.state_delta) or bool(self.stage_to) or self.outcome is not None


class TaskRuntime:
    """Track slots, stage and outcome for one call.

    Deliberately deterministic. The model is told what is still missing; it is
    not trusted to remember what it already asked, because on real calls it
    did not.
    """

    def __init__(self, contract: dict[str, Any] | None = None) -> None:
        contract = contract or {}
        self.task_id = str(contract.get("id", ""))
        self.slots: tuple[SlotSpec, ...] = tuple(
            SlotSpec.parse(entry) for entry in contract.get("inputs_required", []) or []
        )
        self.stages: tuple[StageSpec, ...] = tuple(
            stage
            for stage in (
                StageSpec.parse(entry) for entry in contract.get("conversation_strategy", []) or []
            )
            if stage is not None
        )
        self.stop_conditions: tuple[str, ...] = tuple(
            str(item) for item in contract.get("stop_conditions", []) or []
        )
        self.knowledge: dict[str, str] = {
            str(key): str(value) for key, value in (contract.get("knowledge") or {}).items()
        }
        self.state: dict[str, Any] = {}
        self.stage: str = self.stages[0].name if self.stages else ""
        self.outcome: CallOutcome = CallOutcome.IN_PROGRESS
        self.turns: int = 0

    # ------------------------------------------------------------- observing

    def observe_caller_turn(
        self,
        text: str,
        *,
        excluded_slots: set[str] | frozenset[str] | None = None,
    ) -> TurnActions:
        """Fill whatever this turn answered and report the change."""

        self.turns += 1
        cleaned = (text or "").strip()
        if not cleaned:
            return TurnActions()

        delta: dict[str, Any] = {}
        excluded = excluded_slots or set()
        for slot in self.slots:
            if slot.id in excluded:
                continue
            if slot.id in self.state:
                continue
            captured = slot.match(cleaned)
            if captured:
                delta[slot.id] = captured

        for key, value in delta.items():
            self.state[key] = value

        stage_from = self.stage
        stage_to = self._advance_stage()
        return TurnActions(
            state_delta=delta,
            stage_from=stage_from if stage_to else "",
            stage_to=stage_to,
        )

    def record(self, key: str, value: Any) -> TurnActions:
        """Set one slot explicitly, the way an ADK tool writes session state."""

        if self.state.get(key) == value:
            return TurnActions()
        self.state[key] = value
        stage_from = self.stage
        stage_to = self._advance_stage()
        return TurnActions(
            state_delta={key: value},
            stage_from=stage_from if stage_to else "",
            stage_to=stage_to,
        )

    def set_outcome(self, outcome: CallOutcome) -> TurnActions:
        if self.outcome is outcome:
            return TurnActions()
        self.outcome = outcome
        return TurnActions(outcome=outcome)

    def _advance_stage(self) -> str:
        """Move on only once this stage's required slots are filled."""

        if not self.stages:
            return ""
        names = [stage.name for stage in self.stages]
        if self.stage not in names:
            return ""
        current = self.stages[names.index(self.stage)]
        if not current.requires:
            return ""
        if any(slot not in self.state for slot in current.requires):
            return ""
        index = names.index(self.stage)
        if index + 1 >= len(self.stages):
            return ""
        self.stage = self.stages[index + 1].name
        return self.stage

    def enter_stage(self, name: str) -> TurnActions:
        target = name.strip().upper()
        if not target or target == self.stage:
            return TurnActions()
        if self.stages and target not in {stage.name for stage in self.stages}:
            raise ValueError(f"unknown stage: {name!r}")
        previous, self.stage = self.stage, target
        return TurnActions(stage_from=previous, stage_to=target)

    # -------------------------------------------------------------- steering

    def missing_slots(self) -> tuple[SlotSpec, ...]:
        return tuple(slot for slot in self.slots if slot.id not in self.state)

    def next_missing_slot(self) -> SlotSpec | None:
        missing = self.missing_slots()
        return missing[0] if missing else None

    def current_stage(self) -> StageSpec | None:
        for stage in self.stages:
            if stage.name == self.stage:
                return stage
        return None

    def brief(self) -> str:
        """The steering block injected into the live prompt each turn.

        This is what turns a listed objective into an executed one: the model
        is told what it already knows and the single thing to ask next, so it
        stops re-asking and stops wandering.
        """

        lines: list[str] = []
        stage = self.current_stage()
        if stage:
            lines.append(f"current_stage: {stage.name} — {stage.instruction}")
        if self.state:
            known = "; ".join(f"{key}={value}" for key, value in self.state.items())
            lines.append(f"already_answered (never ask these again): {known}")
        missing = self.missing_slots()
        if missing:
            lines.append("still_needed: " + ", ".join(slot.id for slot in missing))
            nxt = missing[0]
            ask = nxt.question or f"what their {nxt.id.replace('_', ' ')} is"
            lines.append(f"ask_next: {ask}")
        else:
            lines.append("All required information is collected. Move to the next step.")
        if self.knowledge:
            facts = "; ".join(f"{key}: {value}" for key, value in self.knowledge.items())
            lines.append(f"verified_facts (you may state these): {facts}")
        return "\n".join(lines)

    def summary(self) -> dict[str, Any]:
        """Structured disposition, recorded instead of lost at hangup."""

        return {
            "task_id": self.task_id,
            "outcome": self.outcome.value,
            "stage": self.stage,
            "turns": self.turns,
            "collected": dict(self.state),
            "missing": [slot.id for slot in self.missing_slots()],
        }
