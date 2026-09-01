"""Task Engine for PhoneAgent.

Loads and enforces strict Task Contracts (procedures, allowed tools, success criteria).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, ClassVar

import yaml

from ..secure_storage import atomic_write_private

logger = logging.getLogger(__name__)

CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"
# Contracts authored in the Studio live beside the persona, not inside the
# installed package, so an upgrade never overwrites them.
USER_CONTRACTS_DIR = Path.home() / ".config" / "phone-agent" / "tasks"
TASK_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,48}$")


class TaskEngine:
    """Manages active task contracts and validates execution against success criteria."""

    # A contract compiles straight into the live system instruction, so an
    # imported file is untrusted input and both its shape and size are bounded.
    TEXT_FIELDS: ClassVar[tuple[str, ...]] = ("id", "title", "objective")
    LIST_FIELDS: ClassVar[tuple[str, ...]] = (
        "success_criteria",
        "natural_conversation_rules",
        "ground_truth_policy",
        "allowed_tools",
        "approval_required",
        "stop_conditions",
    )
    SLOT_FIELD = "inputs_required"
    STRATEGY_FIELD = "conversation_strategy"
    KNOWLEDGE_FIELD = "knowledge"
    # How this product is actually sold: the delivery the model imitates, and
    # the objections this market really raises. Facts say what is true; these
    # decide whether the caller hears a salesperson or a script.
    PHRASE_FIELD = "sample_phrases"
    OBJECTION_FIELD = "objection_playbook"
    INT_FIELDS: ClassVar[dict[str, tuple[int, int]]] = {
        "spoken_max_words": (5, 120),
        "spoken_sentence_limit": (1, 6),
    }
    MAX_ENTRY_CHARS = 400
    MAX_ENTRIES_PER_FIELD = 40
    MAX_CONTRACT_CHARS = 24_000

    def __init__(
        self,
        contracts_dir: Path | None = None,
        user_contracts_dir: Path | None = None,
    ) -> None:
        self.contracts_dir = contracts_dir or CONTRACTS_DIR
        self.user_contracts_dir = (
            user_contracts_dir if user_contracts_dir is not None else USER_CONTRACTS_DIR
        )
        self._contracts: dict[str, dict[str, Any]] = {}
        self._user_ids: set[str] = set()
        self._load_contracts()

    def _load_dir(self, directory: Path, *, user_authored: bool) -> None:
        if not directory.exists():
            return
        for file in sorted(directory.glob("*.yaml")):
            try:
                with file.open(encoding="utf-8") as stream:
                    data = yaml.safe_load(stream)
                if data and "id" in data:
                    self._contracts[data["id"]] = data
                    if user_authored:
                        self._user_ids.add(str(data["id"]))
            except Exception as exc:
                logger.warning("Failed to load task contract %s: %s", file.name, exc)

    def _load_contracts(self) -> None:
        self._contracts.clear()
        self._user_ids.clear()
        self._load_dir(self.contracts_dir, user_authored=False)
        # Studio-authored contracts win, so an edited copy of a shipped task
        # replaces it rather than appearing twice.
        self._load_dir(self.user_contracts_dir, user_authored=True)

    def is_user_authored(self, task_id: str) -> bool:
        return task_id in self._user_ids

    @classmethod
    def _clean_list(cls, field: str, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError(f"{field} must be a list of lines")
        if len(value) > cls.MAX_ENTRIES_PER_FIELD:
            raise ValueError(f"{field} cannot hold more than {cls.MAX_ENTRIES_PER_FIELD} entries")
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, (str, int, float)):
                raise ValueError(f"{field} must contain only text lines")
            text = str(item).strip()
            if len(text) > cls.MAX_ENTRY_CHARS:
                raise ValueError(f"{field} has a line longer than {cls.MAX_ENTRY_CHARS} characters")
            if text:
                cleaned.append(text)
        return cleaned

    @classmethod
    def _validate_slots(cls, value: Any) -> list[Any]:
        """Accept plain names or declared slots with their own detection.

        A declared slot is what lets the runtime know a question was already
        answered, which is what stops the agent asking it twice.
        """

        if not isinstance(value, list):
            raise ValueError("inputs_required must be a list")
        if len(value) > cls.MAX_ENTRIES_PER_FIELD:
            raise ValueError("inputs_required holds too many entries")
        rendered: list[Any] = []
        seen: set[str] = set()
        for entry in value:
            if isinstance(entry, str):
                slot_id = entry.strip()
                if not slot_id:
                    continue
                rendered.append(slot_id)
            elif isinstance(entry, dict):
                unknown = set(entry) - {"id", "question", "detect"}
                if unknown:
                    raise ValueError(f"unsupported slot keys: {sorted(unknown)}")
                slot_id = str(entry.get("id", "")).strip()
                if not slot_id:
                    raise ValueError("each declared input needs an id")
                slot: dict[str, Any] = {"id": slot_id}
                question = str(entry.get("question", "")).strip()
                if question:
                    if len(question) > cls.MAX_ENTRY_CHARS:
                        raise ValueError(f"question for {slot_id!r} is too long")
                    slot["question"] = question
                detect = cls._clean_list(f"{slot_id}.detect", entry.get("detect", []) or [])
                for pattern in detect:
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        raise ValueError(
                            f"detect pattern for {slot_id!r} is not valid: {exc}"
                        ) from exc
                if detect:
                    slot["detect"] = detect
                rendered.append(slot)
            else:
                raise ValueError("each required input must be a name or an object")
            if slot_id in seen:
                raise ValueError(f"duplicate required input: {slot_id!r}")
            seen.add(slot_id)
        return rendered

    @classmethod
    def _validate_strategy(cls, value: Any, *, valid_slots: set[str]) -> list[Any]:
        """Validate prose policies and structured stage objects without flattening them.

        Studio round-trips used to reject stage objects, so a contract that
        worked from disk failed as soon as it was saved in the Web UI. Keeping
        the structure is also what lets the runtime distinguish OPEN/DISCOVER
        stages from labelled prose such as CALLER PRIORITY and OBJECTIONS.
        """

        if not isinstance(value, list):
            raise ValueError("conversation_strategy must be a list")
        if len(value) > cls.MAX_ENTRIES_PER_FIELD:
            raise ValueError("conversation_strategy holds too many entries")
        rendered: list[Any] = []
        stage_names: set[str] = set()
        for entry in value:
            if not isinstance(entry, dict):
                text = str(entry).strip()
                if len(text) > cls.MAX_ENTRY_CHARS:
                    raise ValueError("conversation_strategy has a line that is too long")
                if text:
                    rendered.append(text)
                continue
            unknown = set(entry) - {"name", "instruction", "requires"}
            if unknown:
                raise ValueError(f"unsupported conversation stage keys: {sorted(unknown)}")
            name = str(entry.get("name", "")).strip().upper()
            instruction = str(entry.get("instruction", "")).strip()
            if not re.fullmatch(r"[A-Z][A-Z0-9 _-]{1,30}", name):
                raise ValueError("a conversation stage needs a short uppercase name")
            if name in stage_names:
                raise ValueError(f"duplicate conversation stage: {name!r}")
            if not instruction:
                raise ValueError(f"conversation stage {name!r} needs an instruction")
            if len(instruction) > cls.MAX_ENTRY_CHARS:
                raise ValueError(f"conversation stage {name!r} instruction is too long")
            raw_requires = entry.get("requires", []) or []
            if not isinstance(raw_requires, list):
                raise ValueError(f"conversation stage {name!r} requires must be a list")
            requires = [str(item).strip() for item in raw_requires if str(item).strip()]
            missing = set(requires) - valid_slots
            if missing:
                raise ValueError(
                    f"conversation stage {name!r} requires unknown inputs: {sorted(missing)}"
                )
            stage = {"name": name, "instruction": instruction}
            if requires:
                stage["requires"] = requires
            rendered.append(stage)
            stage_names.add(name)
        return rendered

    @classmethod
    def _validate_phrases(cls, value: Any) -> dict[str, dict[str, str]]:
        """Spoken examples, keyed by situation then language.

        The model imitates these closely, so they are bounded like any other
        text compiled into the live instruction.
        """

        if not isinstance(value, dict):
            raise ValueError("sample_phrases must be an object of situation to wording")
        if len(value) > cls.MAX_ENTRIES_PER_FIELD:
            raise ValueError("sample_phrases holds too many situations")
        rendered: dict[str, dict[str, str]] = {}
        for situation, wording in value.items():
            name = str(situation).strip()
            if not name:
                continue
            if isinstance(wording, str):
                wording = {"en": wording}
            if not isinstance(wording, dict):
                raise ValueError(f"sample phrase for {name!r} must be text or a language map")
            languages: dict[str, str] = {}
            for language, phrase in wording.items():
                code = str(language).strip().lower()[:5]
                text = str(phrase).strip()
                if not code or not text:
                    continue
                if len(text) > cls.MAX_ENTRY_CHARS:
                    raise ValueError(f"sample phrase for {name!r} is too long")
                languages[code] = text
            if languages:
                rendered[name] = languages
        return rendered

    @classmethod
    def _validate_objections(cls, value: Any) -> list[dict[str, str]]:
        """Objections this market raises, each with how to meet it."""

        if not isinstance(value, list):
            raise ValueError("objection_playbook must be a list")
        if len(value) > cls.MAX_ENTRIES_PER_FIELD:
            raise ValueError("objection_playbook holds too many entries")
        rendered: list[dict[str, str]] = []
        for entry in value:
            if not isinstance(entry, dict):
                raise ValueError("each objection must be an object")
            unknown = set(entry) - {"objection", "answer", "source"}
            if unknown:
                raise ValueError(f"unsupported objection keys: {sorted(unknown)}")
            objection = str(entry.get("objection", "")).strip()
            answer = str(entry.get("answer", "")).strip()
            if not objection or not answer:
                continue
            for label, text in (("objection", objection), ("answer", answer)):
                if len(text) > cls.MAX_ENTRY_CHARS:
                    raise ValueError(f"objection {label} is too long: {objection[:40]!r}")
            item = {"objection": objection, "answer": answer}
            source = str(entry.get("source", "")).strip()
            if source:
                item["source"] = source[: cls.MAX_ENTRY_CHARS]
            rendered.append(item)
        return rendered

    @classmethod
    def _validate_knowledge(cls, value: Any) -> dict[str, str]:
        """Facts the agent may state aloud.

        Without this the ground-truth policy forbids inventing prices and
        nothing supplies real ones, so the agent cannot answer "how much?" at
        all.
        """

        if not isinstance(value, dict):
            raise ValueError("knowledge must be an object of fact name to value")
        if len(value) > cls.MAX_ENTRIES_PER_FIELD:
            raise ValueError("knowledge holds too many facts")
        rendered: dict[str, str] = {}
        for key, fact in value.items():
            name = str(key).strip()
            text = str(fact).strip()
            if not name or not text:
                continue
            if len(text) > cls.MAX_ENTRY_CHARS:
                raise ValueError(f"knowledge value for {name!r} is too long")
            rendered[name] = text
        return rendered

    @classmethod
    def validate_contract(cls, data: Any) -> dict[str, Any]:
        """Validate an authored or imported contract before it can be used."""

        if not isinstance(data, dict):
            raise ValueError("a task contract must be a JSON object")
        allowed = (
            set(cls.TEXT_FIELDS)
            | set(cls.LIST_FIELDS)
            | set(cls.INT_FIELDS)
            | {
                "opening_greeting",
                cls.SLOT_FIELD,
                cls.STRATEGY_FIELD,
                cls.KNOWLEDGE_FIELD,
                cls.PHRASE_FIELD,
                cls.OBJECTION_FIELD,
            }
        )
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unsupported task fields: {sorted(unknown)}")

        task_id = str(data.get("id", "")).strip()
        if not TASK_ID_RE.fullmatch(task_id):
            raise ValueError(
                "task id must be lowercase letters, digits and underscores (3-49 characters)"
            )
        title = str(data.get("title", "")).strip()
        objective = str(data.get("objective", "")).strip()
        if not title:
            raise ValueError("task title is required")
        if not objective:
            raise ValueError("task objective is required")

        contract: dict[str, Any] = {"id": task_id, "title": title, "objective": objective}

        greeting = data.get("opening_greeting", {})
        if greeting:
            if not isinstance(greeting, dict):
                raise ValueError("opening_greeting must be an object keyed by language")
            unsupported = set(greeting) - {"en", "fr"}
            if unsupported:
                raise ValueError(f"opening_greeting supports en and fr only: {sorted(unsupported)}")
            rendered = {}
            for language, text in greeting.items():
                line = str(text).strip()
                if len(line) > cls.MAX_ENTRY_CHARS:
                    raise ValueError(f"opening_greeting.{language} is too long")
                if line:
                    rendered[language] = line
            if rendered:
                contract["opening_greeting"] = rendered

        for field, (low, high) in cls.INT_FIELDS.items():
            if field not in data:
                continue
            try:
                number = int(data[field])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field} must be a whole number") from exc
            if not low <= number <= high:
                raise ValueError(f"{field} must be between {low} and {high}")
            contract[field] = number

        for field in cls.LIST_FIELDS:
            if field in data:
                cleaned = cls._clean_list(field, data[field])
                if cleaned:
                    contract[field] = cleaned

        if cls.SLOT_FIELD in data:
            contract[cls.SLOT_FIELD] = cls._validate_slots(data[cls.SLOT_FIELD])
        if cls.STRATEGY_FIELD in data:
            slots = {
                str(entry.get("id", "")).strip() if isinstance(entry, dict) else str(entry).strip()
                for entry in contract.get(cls.SLOT_FIELD, [])
            }
            contract[cls.STRATEGY_FIELD] = cls._validate_strategy(
                data[cls.STRATEGY_FIELD], valid_slots=slots
            )
        if cls.KNOWLEDGE_FIELD in data:
            contract[cls.KNOWLEDGE_FIELD] = cls._validate_knowledge(data[cls.KNOWLEDGE_FIELD])
        if cls.PHRASE_FIELD in data:
            contract[cls.PHRASE_FIELD] = cls._validate_phrases(data[cls.PHRASE_FIELD])
        if cls.OBJECTION_FIELD in data:
            contract[cls.OBJECTION_FIELD] = cls._validate_objections(data[cls.OBJECTION_FIELD])

        total = len(json.dumps(contract, ensure_ascii=False))
        if total > cls.MAX_CONTRACT_CHARS:
            raise ValueError(
                f"task contract is too large to compile ({total} characters, "
                f"limit {cls.MAX_CONTRACT_CHARS})"
            )
        return contract

    def save_contract(self, data: Any) -> dict[str, Any]:
        """Validate and persist a Studio-authored contract."""

        contract = self.validate_contract(data)
        target = self.user_contracts_dir / f"{contract['id']}.yaml"
        payload = yaml.safe_dump(contract, allow_unicode=True, sort_keys=False)
        atomic_write_private(target, payload)
        self._load_contracts()
        return contract

    def delete_contract(self, task_id: str) -> bool:
        """Remove a Studio-authored contract; shipped ones cannot be deleted."""

        if task_id not in self._user_ids:
            raise ValueError(f"{task_id!r} is not a Studio-authored task")
        target = self.user_contracts_dir / f"{task_id}.yaml"
        if target.exists():
            target.unlink()
        self._load_contracts()
        return True

    def get_contract(self, task_id: str) -> dict[str, Any] | None:
        """Retrieve a loaded task contract by ID."""
        return self._contracts.get(task_id)

    def get_all_contracts(self) -> list[dict[str, Any]]:
        """List all available task contracts."""
        return list(self._contracts.values())

    def validate_action_permission(self, task_id: str, action_name: str) -> tuple[bool, str]:
        """Check if an action or tool is permitted under the task contract."""
        contract = self.get_contract(task_id)
        if not contract:
            return False, f"Unknown task contract: {task_id}"

        approvals = contract.get("approval_required", [])
        if action_name in approvals:
            return (
                False,
                f"Action '{action_name}' requires explicit human or security authorization",
            )

        allowed = set(contract.get("allowed_tools", []))
        if action_name not in allowed:
            return False, f"Action '{action_name}' is not allowed by task contract '{task_id}'"

        return True, "Permitted"

    def require_contract(self, task_id: str) -> dict[str, Any]:
        contract = self.get_contract(task_id)
        if contract is None:
            raise ValueError(f"unknown task contract: {task_id}")
        return dict(contract)
