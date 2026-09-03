"""Canonical AgentPackageV1 specification and compiler.

Governed by Milestone 3 (M3-01 through M3-12):
Defines the universal AgentPackageV1 format allowing any lawful domain
to describe an agent without source code changes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AgentMetadataV1(StrictModel):
    package_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(default="", max_length=1000)
    domain: str = Field(default="general", min_length=1, max_length=64)


class ImmutableIdentityV1(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=240)
    mission: str = Field(min_length=1, max_length=2000)
    disclosure: str = Field(default="", max_length=1000)
    values: list[str] = Field(default_factory=list)
    forbidden_behavior: list[str] = Field(default_factory=list)


class ProductCatalogItemV1(StrictModel):
    item_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    price: float = Field(ge=0.0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    available: bool = True
    terms: str = Field(default="", max_length=1000)


class UniversalTaskContractV1(StrictModel):
    task_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=3, max_length=2000)
    allowed_tools: list[str] = Field(default_factory=list)
    escalation_conditions: list[str] = Field(default_factory=list)


class CapabilityManifestV1(StrictModel):
    tool_name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1000)
    risk_level: Literal["low", "medium", "high"] = "low"
    requires_human_approval: bool = False


class KnowledgeManifestV1(StrictModel):
    topic: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=50_000)
    trust_level: Literal["verified", "external", "unverified"] = "verified"


class MemoryManifestV1(StrictModel):
    scope: Literal["call", "caller", "global"] = "call"
    retention_days: int = Field(default=30, ge=0, le=3650)
    encrypted: bool = True


class VoiceProfileV1(StrictModel):
    tts_provider: str = Field(default="supertonic", min_length=1, max_length=100)
    tts_model: str = Field(default="supertonic-2", min_length=1, max_length=240)
    voice_id: str = Field(default="M1", min_length=1, max_length=240)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


class EvaluationScenarioV1(StrictModel):
    scenario_id: str = Field(min_length=1, max_length=64)
    input_transcript: str = Field(min_length=1, max_length=1000)
    expected_outcomes: list[str] = Field(default_factory=list)
    prohibited_outcomes: list[str] = Field(default_factory=list)


class AgentPackageV1(StrictModel):
    """Canonical Universal Agent Package Specification (v1)."""

    schema_version: Literal[1] = 1
    metadata: AgentMetadataV1
    identity: ImmutableIdentityV1
    catalog: list[ProductCatalogItemV1] = Field(default_factory=list)
    tasks: list[UniversalTaskContractV1] = Field(default_factory=list)
    capabilities: list[CapabilityManifestV1] = Field(default_factory=list)
    knowledge: list[KnowledgeManifestV1] = Field(default_factory=list)
    memory: MemoryManifestV1 = Field(default_factory=MemoryManifestV1)
    voice: VoiceProfileV1 = Field(default_factory=VoiceProfileV1)
    evaluations: list[EvaluationScenarioV1] = Field(default_factory=list)
    signature: str = Field(default="")

    def compute_digest(self) -> str:
        """Byte-deterministic package digest excluding signature (M3-11)."""
        data = self.model_dump(mode="json", exclude={"signature"})
        payload = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def sign(self, secret_key: str = "mock_key") -> None:
        digest = self.compute_digest()
        self.signature = hashlib.sha256(f"{digest}:{secret_key}".encode()).hexdigest()

    def verify_signature(self, secret_key: str = "mock_key") -> bool:
        digest = self.compute_digest()
        expected = hashlib.sha256(f"{digest}:{secret_key}".encode()).hexdigest()
        return self.signature == expected
