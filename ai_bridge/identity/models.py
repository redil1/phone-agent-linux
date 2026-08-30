"""Strict contracts for the provider-neutral PhoneAgent Identity Kernel."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def content_hash(value: BaseModel | dict[str, Any]) -> str:
    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    payload = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LanguageCode(StrEnum):
    EN = "en"
    FR = "fr"


class MemoryKind(StrEnum):
    SELF = "self"
    HUMAN = "human"
    BUSINESS = "business"
    PROCEDURAL = "procedural"
    EPISODIC_INDEX = "episodic_index"


class MemorySource(StrEnum):
    MIGRATED = "migrated"
    OPERATOR = "operator"
    USER_EXPLICIT = "user_explicit"
    AGENT_INFERRED = "agent_inferred"
    SYSTEM = "system"


class RevisionState(StrEnum):
    DRAFT = "draft"
    EVALUATED = "evaluated"
    APPROVED = "approved"
    ACTIVATED = "activated"
    REJECTED = "rejected"


class VoiceStyle(StrictModel):
    tone: Literal["warm", "neutral", "direct", "calm", "confident"] = "warm"
    formality: Literal["casual", "professional", "formal"] = "professional"
    verbosity: Literal["terse", "concise", "balanced"] = "concise"
    empathy: float = Field(default=0.9, ge=0, le=1)
    assertiveness: float = Field(default=0.8, ge=0, le=1)
    humor: float = Field(default=0.1, ge=0, le=1)
    pace: Literal["measured", "natural", "brisk"] = "natural"
    max_words_per_turn: int = Field(default=30, ge=5, le=60)
    max_sentences_per_turn: int = Field(default=2, ge=1, le=3)
    ask_one_question: bool = True
    allow_code_switching: bool = True
    spoken_only: bool = True


class IdentityCore(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    role: str = Field(min_length=3, max_length=240)
    mission: str = Field(min_length=20, max_length=2_000)
    organization: str = Field(default="", max_length=160)
    ai_disclosure: dict[LanguageCode, str] = Field(default_factory=dict)
    values: list[str] = Field(min_length=3, max_length=24)
    decision_priorities: list[str] = Field(min_length=2, max_length=20)
    hard_boundaries: list[str] = Field(default_factory=list, max_length=40)
    forbidden_behaviors: list[str] = Field(default_factory=list, max_length=60)
    topics: list[str] = Field(default_factory=list, max_length=80)

    @field_validator(
        "values", "decision_priorities", "hard_boundaries", "forbidden_behaviors", "topics"
    )
    @classmethod
    def unique_bounded_lines(cls, values: list[str]) -> list[str]:
        output: list[str] = []
        for value in values:
            rendered = str(value).strip()
            if not rendered or len(rendered) > 300:
                raise ValueError("identity list entries must contain 1-300 characters")
            if rendered not in output:
                output.append(rendered)
        return output


class BehaviorExample(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    language: LanguageCode
    situation: str = Field(min_length=5, max_length=500)
    caller_input: str = Field(min_length=1, max_length=500)
    ideal_response: str = Field(min_length=1, max_length=500)
    anti_response: str = Field(default="", max_length=500)
    rationale: str = Field(min_length=5, max_length=500)
    expected_skill: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    tags: list[str] = Field(default_factory=list, max_length=12)


class EvaluationCase(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    category: Literal[
        "identity", "multilingual", "forbidden_behavior", "tool_selection", "naturalness"
    ]
    language: LanguageCode
    user_input: str = Field(min_length=1, max_length=800)
    expected_contains: list[str] = Field(default_factory=list, max_length=12)
    expected_any: list[str] = Field(default_factory=list, max_length=20)
    forbidden_contains: list[str] = Field(default_factory=list, max_length=20)
    expected_skill: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    reference_response: str = Field(min_length=1, max_length=800)


class IdentityProfile(StrictModel):
    schema_version: Literal[1] = 1
    identity_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    version: int = Field(default=1, ge=1)
    core: IdentityCore
    voice: VoiceStyle = Field(default_factory=VoiceStyle)
    default_language: LanguageCode = LanguageCode.EN
    supported_languages: list[LanguageCode] = Field(
        default_factory=lambda: [LanguageCode.EN, LanguageCode.FR], min_length=1, max_length=2
    )
    examples: list[BehaviorExample] = Field(default_factory=list, max_length=80)
    evaluation_cases: list[EvaluationCase] = Field(default_factory=list, max_length=100)
    enabled_skills: list[str] = Field(default_factory=list, max_length=64)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    @field_validator("enabled_skills")
    @classmethod
    def validate_skill_ids(cls, values: list[str]) -> list[str]:
        output: list[str] = []
        for value in values:
            if not __import__("re").fullmatch(r"[a-z][a-z0-9_-]{2,63}", value):
                raise ValueError("enabled skill id is invalid")
            if value not in output:
                output.append(value)
        return output

    @model_validator(mode="after")
    def validate_languages(self) -> IdentityProfile:
        if self.default_language not in self.supported_languages:
            raise ValueError("default language must be supported")
        if len(set(self.supported_languages)) != len(self.supported_languages):
            raise ValueError("supported languages must be unique")
        return self


class MemoryBlock(StrictModel):
    schema_version: Literal[1] = 1
    block_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    kind: MemoryKind
    label: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=6_000)
    mutable: bool = True
    priority: int = Field(default=50, ge=0, le=100)
    source: MemorySource = MemorySource.OPERATOR
    confidence: float = Field(default=1.0, ge=0, le=1)
    caller_scope_hash: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{16,64}$")
    valid_from: str = Field(default_factory=utc_now)
    valid_until: str | None = None
    updated_at: str = Field(default_factory=utc_now)


class MemoryProposal(StrictModel):
    schema_version: Literal[1] = 1
    proposal_id: str = Field(pattern=r"^mem_[a-f0-9]{24}$")
    block: MemoryBlock
    evidence: str = Field(min_length=3, max_length=2_000)
    state: Literal["pending", "approved", "rejected"] = "pending"
    proposed_at: str = Field(default_factory=utc_now)
    decided_by: str | None = Field(default=None, max_length=120)
    decided_at: str | None = None


class EvaluationFinding(StrictModel):
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,100}$")
    category: str = Field(min_length=2, max_length=80)
    severity: Literal["info", "warning", "critical"]
    passed: bool
    message: str = Field(min_length=1, max_length=1_000)


class EvaluationReport(StrictModel):
    schema_version: Literal[1] = 1
    profile_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    score: float = Field(ge=0, le=100)
    passed: bool
    findings: list[EvaluationFinding] = Field(max_length=300)
    categories: dict[str, float]
    evaluated_at: str = Field(default_factory=utc_now)
    evaluator_version: str = "identity-eval-v1"


class RevisionApproval(StrictModel):
    approved_by: str = Field(min_length=2, max_length=120)
    approved_at: str = Field(default_factory=utc_now)
    profile_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class IdentityRevision(StrictModel):
    schema_version: Literal[1] = 1
    revision_id: str = Field(pattern=r"^rev_[a-f0-9]{24}$")
    state: RevisionState = RevisionState.DRAFT
    base_profile_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    candidate_profile_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    candidate: IdentityProfile
    reason: str = Field(min_length=3, max_length=1_000)
    created_by: str = Field(min_length=2, max_length=120)
    created_at: str = Field(default_factory=utc_now)
    evaluation: EvaluationReport | None = None
    approval: RevisionApproval | None = None
    activated_at: str | None = None
