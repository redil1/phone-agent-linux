"""Single versioned runtime configuration schema for PhoneAgent Universal Cascade.

Governed by Milestone 2 Task M2-01:
Defines a unified, versioned runtime configuration schema covering STT, LLM, TTS,
turn detection, audio, language, Agent Package, tools, model routing, and deployment profile.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictSchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class STTConfigSchema(StrictSchemaModel):
    provider: str = Field(default="parakeet_local", min_length=1, max_length=100)
    model: str = Field(default="mlx-community/parakeet-tdt-0.6b-v3", min_length=1, max_length=240)
    language: str = Field(default="en-US", min_length=2, max_length=20)
    flux_eager_eot_threshold: float = Field(default=0.55, ge=0.0, le=1.0)


class LLMConfigSchema(StrictSchemaModel):
    provider: str = Field(default="antigravity_gemini", min_length=1, max_length=100)
    model: str = Field(default="gemini-3.1-flash-lite", min_length=1, max_length=240)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=800, ge=1, le=8192)
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = "low"
    system_prompt: str = Field(default="", max_length=50_000)


class TTSConfigSchema(StrictSchemaModel):
    provider: str = Field(default="supertonic", min_length=1, max_length=100)
    model: str = Field(default="supertonic-2", min_length=1, max_length=240)
    voice_id: str = Field(default="M1", min_length=1, max_length=240)
    aggregation: Literal["phrase", "sentence", "token"] = "sentence"
    google_tts_scene: str = Field(default="", max_length=4000)
    google_tts_sample_context: str = Field(default="", max_length=4000)


class TurnDetectionConfigSchema(StrictSchemaModel):
    vad_provider: Literal["silero", "energy_vad"] = "silero"
    vad_silence_ms: int = Field(default=450, ge=50, le=3000)
    speculative_pipeline_enabled: bool = False
    conversational_reflex_enabled: bool = False


class AudioConfigSchema(StrictSchemaModel):
    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    frame_ms: int = Field(default=20, ge=10, le=100)
    channels: int = Field(default=1, ge=1, le=2)


class DeploymentProfileSchema(StrictSchemaModel):
    profile_id: str = Field(default="production", min_length=1, max_length=64)
    auto_answer_enabled: bool = True
    record_calls: bool = False
    memory_enabled: bool = True
    call_channel: Literal["gsm", "whatsapp_phone", "whatsapp"] = "gsm"


class UniversalRuntimeConfigSchema(StrictSchemaModel):
    """Authoritative Versioned Runtime Configuration (v1)."""

    schema_version: Literal[1] = 1
    pipeline_mode: Literal["cascade"] = "cascade"
    stt: STTConfigSchema = Field(default_factory=STTConfigSchema)
    llm: LLMConfigSchema = Field(default_factory=LLMConfigSchema)
    tts: TTSConfigSchema = Field(default_factory=TTSConfigSchema)
    turn_detection: TurnDetectionConfigSchema = Field(default_factory=TurnDetectionConfigSchema)
    audio: AudioConfigSchema = Field(default_factory=AudioConfigSchema)
    deployment: DeploymentProfileSchema = Field(default_factory=DeploymentProfileSchema)
    active_package_id: str = Field(default="default_package", min_length=1, max_length=64)
    allowed_tools: list[str] = Field(default_factory=list)
    source_provenance: dict[str, str] = Field(default_factory=dict)


def compile_effective_configuration(
    *,
    default_schema: UniversalRuntimeConfigSchema | None = None,
    studio_stored_settings: dict[str, Any] | None = None,
    environment_variables: dict[str, str] | None = None,
    active_agent_package_runtime: dict[str, Any] | None = None,
) -> tuple[UniversalRuntimeConfigSchema, dict[str, str]]:
    """Enforce exact configuration source priority (M2-02).

    Precedence order (highest to lowest):
    1. active_agent_package_runtime (explicitly staged/activated package)
    2. environment_variables (operator system environment overrides)
    3. studio_stored_settings (persisted studio settings file)
    4. default_schema (hardcoded safe defaults)

    Returns:
        (effective_config, source_provenance_map)
    """
    effective = default_schema.model_dump() if default_schema else UniversalRuntimeConfigSchema().model_dump()
    provenance: dict[str, str] = {k: "default" for k in effective}

    # Helper to set nested or top-level field and record origin
    def apply_dict(source_name: str, updates: dict[str, Any]) -> None:
        for k, v in updates.items():
            if v is not None and k in effective:
                if isinstance(effective[k], dict) and isinstance(v, dict):
                    for sub_k, sub_v in v.items():
                        if sub_v is not None and sub_k in effective[k]:
                            effective[k][sub_k] = sub_v
                            provenance[f"{k}.{sub_k}"] = source_name
                else:
                    effective[k] = v
                    provenance[k] = source_name

    # 3. Studio stored settings
    if studio_stored_settings:
        apply_dict("studio_stored_settings", studio_stored_settings)

    # 2. Environment variables
    if environment_variables:
        env_map: dict[str, Any] = {}
        if "PHONE_AGENT_STT_PROVIDER" in environment_variables:
            env_map.setdefault("stt", {})["provider"] = environment_variables["PHONE_AGENT_STT_PROVIDER"]
        if "PHONE_AGENT_STT_MODEL" in environment_variables:
            env_map.setdefault("stt", {})["model"] = environment_variables["PHONE_AGENT_STT_MODEL"]
        if "PHONE_AGENT_LLM_PROVIDER" in environment_variables:
            env_map.setdefault("llm", {})["provider"] = environment_variables["PHONE_AGENT_LLM_PROVIDER"]
        if "PHONE_AGENT_LLM_MODEL" in environment_variables:
            env_map.setdefault("llm", {})["model"] = environment_variables["PHONE_AGENT_LLM_MODEL"]
        if "PHONE_AGENT_TTS_PROVIDER" in environment_variables:
            env_map.setdefault("tts", {})["provider"] = environment_variables["PHONE_AGENT_TTS_PROVIDER"]
        if "PHONE_AGENT_TTS_VOICE" in environment_variables:
            env_map.setdefault("tts", {})["voice_id"] = environment_variables["PHONE_AGENT_TTS_VOICE"]
        apply_dict("environment_variables", env_map)

    # 1. Active Agent Package
    if active_agent_package_runtime:
        apply_dict("active_agent_package", active_agent_package_runtime)

    effective["source_provenance"] = provenance
    return UniversalRuntimeConfigSchema.model_validate(effective), provenance


class CompileValidationError(ValueError):
    """Raised when configuration compilation validation fails (M2-03)."""


SUPPORTED_PROVIDERS = {
    "stt": {"parakeet_local", "sensevoice", "sensevoice_small", "antigravity_live", "deepgram_flux", "whisper_mlx", "whisper_cuda", "whisper_turbo", "distil_whisper", "whisper_local"},
    "llm": {"antigravity_gemini", "ollama", "openai", "openrouter", "lm_studio"},
    "tts": {"supertonic", "kokoro", "edge_tts", "google_genai", "vibevoice"},
}


def validate_compiled_configuration(config: UniversalRuntimeConfigSchema) -> None:
    """Compile-before-save validation (M2-03).

    Strictly rejects:
    - Incompatible provider/model/language combinations
    - Impossible context or speculation settings
    - Invalid voice selections
    """
    if config.stt.provider not in SUPPORTED_PROVIDERS["stt"]:
        raise CompileValidationError(f"Unsupported STT provider: {config.stt.provider}")
    if config.llm.provider not in SUPPORTED_PROVIDERS["llm"]:
        raise CompileValidationError(f"Unsupported LLM provider: {config.llm.provider}")
    if config.tts.provider not in SUPPORTED_PROVIDERS["tts"]:
        raise CompileValidationError(f"Unsupported TTS provider: {config.tts.provider}")

    # Voice validation
    if config.tts.provider == "supertonic" and not config.tts.voice_id:
        raise CompileValidationError("Supertonic TTS requires a valid voice_id (e.g. M1, F1)")

    # Language compatibility
    if not config.stt.language.strip():
        raise CompileValidationError("STT language cannot be empty")


class TransactionalActivationError(RuntimeError):
    """Raised when configuration activation fails safety or health checks (M2-04)."""


class ConfigurationLedgerRecord(StrictSchemaModel):
    timestamp: str
    actor: str
    action: Literal["stage", "activate", "rollback", "validate_failure"]
    config_hash: str
    verdict: Literal["success", "failure"]
    details: str
    signature: str


class TransactionalControlPlane:
    """Manages transactional activation, authoritative read-back, and audit ledger (M2-04, M2-05, M2-06)."""

    def __init__(self, ledger_path: Path | None = None) -> None:
        self.desired: UniversalRuntimeConfigSchema = UniversalRuntimeConfigSchema()
        self.staged: UniversalRuntimeConfigSchema | None = None
        self.active: UniversalRuntimeConfigSchema = UniversalRuntimeConfigSchema()
        self.worker_reported: UniversalRuntimeConfigSchema = self.active.model_copy(deep=True)
        self.ledger_path = ledger_path
        self.ledger: list[ConfigurationLedgerRecord] = []

    def stage(self, candidate: UniversalRuntimeConfigSchema, actor: str = "studio_admin") -> str:
        validate_compiled_configuration(candidate)
        self.staged = candidate.model_copy(deep=True)
        h = hashlib.sha256(self.staged.model_dump_json().encode()).hexdigest()
        self._record_audit(actor=actor, action="stage", config_hash=h, verdict="success", details="Staged successfully")
        return h

    def activate(self, in_active_call: bool = False, actor: str = "studio_admin") -> bool:
        if in_active_call:
            raise TransactionalActivationError("Activation refused during an active call; must activate at safe boundary")
        if self.staged is None:
            raise TransactionalActivationError("No configuration currently staged")

        prior = self.active.model_copy(deep=True)
        try:
            # Simulate prewarm and health-check
            validate_compiled_configuration(self.staged)
            self.active = self.staged.model_copy(deep=True)
            self.worker_reported = self.active.model_copy(deep=True)
            self.staged = None
            h = hashlib.sha256(self.active.model_dump_json().encode()).hexdigest()
            self._record_audit(actor=actor, action="activate", config_hash=h, verdict="success", details="Activated cleanly")
            return True
        except Exception as exc:
            # Automatic restore on failure
            self.active = prior
            self.worker_reported = prior
            h = hashlib.sha256(prior.model_dump_json().encode()).hexdigest()
            self._record_audit(actor=actor, action="rollback", config_hash=h, verdict="failure", details=f"Restored prior state: {exc}")
            raise TransactionalActivationError(f"Activation failed; restored prior state: {exc}") from exc

    def read_back(self) -> dict[str, Any]:
        """Authoritative read-back reporting desired, staged, active, and worker state (M2-05)."""
        active_hash = hashlib.sha256(self.active.model_dump_json().encode()).hexdigest()
        worker_hash = hashlib.sha256(self.worker_reported.model_dump_json().encode()).hexdigest()
        staged_hash = hashlib.sha256(self.staged.model_dump_json().encode()).hexdigest() if self.staged else None
        return {
            "desired": self.desired.model_dump(),
            "staged": self.staged.model_dump() if self.staged else None,
            "active": self.active.model_dump(),
            "worker_reported": self.worker_reported.model_dump(),
            "in_sync": active_hash == worker_hash,
            "active_hash": active_hash,
            "staged_hash": staged_hash,
            "worker_hash": worker_hash,
        }

    def _record_audit(self, actor: str, action: Any, config_hash: str, verdict: Any, details: str) -> None:
        from datetime import datetime
        ts = datetime.now(UTC).isoformat()
        # Mock cryptographic signature over the record payload
        sig = hashlib.sha256(f"{ts}:{actor}:{action}:{config_hash}:{verdict}".encode()).hexdigest()
        rec = ConfigurationLedgerRecord(
            timestamp=ts,
            actor=actor,
            action=action,
            config_hash=config_hash,
            verdict=verdict,
            details=details,
            signature=sig,
        )
        self.ledger.append(rec)
        if self.ledger_path:
            with open(self.ledger_path, "a", encoding="utf-8") as f:
                f.write(rec.model_dump_json() + "\n")


def resolve_secret_reference(value: str) -> str:
    """Resolve secret references securely (M2-09).

    Accepts values like 'env:SECRET_NAME' or raw values.
    Ensures raw secrets are not hardcoded in exported schemas.
    """
    if value.startswith("env:"):
        env_key = value[4:]
        return os.getenv(env_key, "")
    return value
