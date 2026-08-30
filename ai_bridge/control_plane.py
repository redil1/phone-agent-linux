"""Versioned external-agent control contracts for PhoneAgent.

The control plane changes declarative configuration only. It deliberately has
no filesystem, shell, Android, codec, PCM, or media-pipeline operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .identity.models import IdentityProfile, MemoryBlock
from .identity.skills import SkillDraft
from .secure_storage import atomic_write_private, harden_private_file

DEFAULT_CONTROL_PLANE_ROOT = Path.home() / ".config" / "phone-agent" / "control-plane"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


class StrictControlModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RuntimeControl(StrictControlModel):
    """Call-quality and provider parameters safe to change between calls."""

    pipeline_mode: Literal["cascade", "s2s_chatgpt_realtime"] = "s2s_chatgpt_realtime"
    call_channel: Literal["gsm", "whatsapp_phone", "whatsapp"] = "gsm"
    stt_provider: str = Field(default="parakeet_local", min_length=1, max_length=100)
    stt_model: str = Field(default="mlx-community/parakeet-tdt-0.6b-v3", max_length=240)
    stt_language: str = Field(default="en-US", min_length=2, max_length=20)
    llm_provider: str = Field(default="antigravity_gemini", min_length=1, max_length=100)
    llm_model: str = Field(default="gemini-3.1-flash-lite", max_length=240)
    tts_provider: str = Field(default="supertonic", min_length=1, max_length=100)
    tts_model: str = Field(default="supertonic-2", max_length=240)
    tts_voice_id: str = Field(default="M1", min_length=1, max_length=240)
    tts_aggregation: Literal["phrase", "sentence", "token"] = "sentence"
    google_tts_scene: str = Field(default="", max_length=4_000)
    google_tts_sample_context: str = Field(default="", max_length=4_000)
    speculative_pipeline_enabled: bool = True
    conversational_reflex_enabled: bool = True
    auto_answer_enabled: bool = True
    whatsapp_country_code: str = Field(default="212", pattern=r"^[0-9]{1,4}$")
    chatgpt_realtime_voice: str = Field(default="marin", max_length=40)
    chatgpt_realtime_model: str = Field(default="auto", max_length=120)
    chatgpt_realtime_transport: Literal["websocket", "webrtc"] = "websocket"
    chatgpt_realtime_reasoning_effort: Literal[
        "minimal", "low", "medium", "high", "xhigh"
    ] = "low"
    chatgpt_realtime_transcription_model: str = Field(
        default="gpt-live-transcribe", max_length=120
    )
    chatgpt_realtime_input_languages: list[str] = Field(
        default_factory=lambda: ["en", "fr"], min_length=1, max_length=2
    )
    chatgpt_realtime_noise_reduction: Literal["off", "near_field", "far_field"] = "off"
    chatgpt_realtime_vad_mode: Literal["server_vad", "semantic_vad"] = "server_vad"
    chatgpt_realtime_vad_eagerness: Literal["low", "medium", "high", "auto"] = "medium"
    chatgpt_realtime_vad_silence_ms: int = Field(default=700, ge=100, le=5_000)
    chatgpt_realtime_idle_timeout_ms: int = Field(default=8_000, ge=0, le=60_000)
    chatgpt_realtime_speed: float = Field(default=1.05, ge=0.8, le=2.0)
    system_prompt: str = Field(default="", max_length=12_000)

    @field_validator("chatgpt_realtime_input_languages")
    @classmethod
    def _languages(cls, values: list[str]) -> list[str]:
        rendered = list(dict.fromkeys(str(value).strip().lower() for value in values))
        if not rendered or not set(rendered) <= {"en", "fr"}:
            raise ValueError("Realtime input languages currently support en and fr")
        return rendered


class AgentPackage(StrictControlModel):
    schema_version: Literal[1] = 1
    package_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    display_name: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=3, max_length=2_000)
    identity: IdentityProfile
    task: dict[str, Any]
    runtime: RuntimeControl
    skills: list[SkillDraft] = Field(default_factory=list, max_length=64)
    memory_blocks: list[MemoryBlock] = Field(default_factory=list, max_length=32)
    tools: dict[str, Any]
    openwa: dict[str, Any]
    web_research: dict[str, Any]
    business: dict[str, Any]
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("task", "tools", "openwa", "web_research", "business")
    @classmethod
    def _bounded_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, ensure_ascii=False, default=str)) > 96_000:
            raise ValueError("AgentPackage component exceeds its size bound")
        return value

    @field_validator("memory_blocks")
    @classmethod
    def _only_mutable_memory(cls, values: list[MemoryBlock]) -> list[MemoryBlock]:
        if any(not block.mutable or block.kind.value == "self" for block in values):
            raise ValueError("AgentPackage may contain only mutable non-self memory blocks")
        ids = [block.block_id for block in values]
        if len(ids) != len(set(ids)):
            raise ValueError("AgentPackage memory block ids must be unique")
        return values

    @field_validator("labels")
    @classmethod
    def _labels(cls, values: dict[str, str]) -> dict[str, str]:
        if len(values) > 32:
            raise ValueError("AgentPackage has too many labels")
        result: dict[str, str] = {}
        for key, value in values.items():
            name = str(key).strip()
            text = str(value).strip()
            if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", name) or len(text) > 240:
                raise ValueError("AgentPackage label is invalid")
            result[name] = text
        return result

    @model_validator(mode="after")
    def _total_size(self) -> AgentPackage:
        size = len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False))
        if size > 400_000:
            raise ValueError("AgentPackage exceeds the 400,000-character transport bound")
        return self


class PackageValidation(StrictControlModel):
    valid: bool
    package_hash: str
    effective_state_hash: str
    checks: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    warnings: list[str] = Field(default_factory=list, max_length=100)


class DeploymentRecord(StrictControlModel):
    schema_version: Literal[1] = 1
    deployment_id: str = Field(pattern=r"^dep_[a-f0-9]{24}$")
    state: Literal["staged", "activating", "active", "superseded", "failed"] = "staged"
    package_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    base_state_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    package: AgentPackage
    validation: PackageValidation
    reason: str = Field(min_length=3, max_length=1_000)
    created_by: str = Field(min_length=2, max_length=120)
    created_at: str = Field(default_factory=_now)
    activated_at: str | None = None
    failure: str = Field(default="", max_length=1_000)


class ControlPlaneError(RuntimeError):
    pass


class ControlPlaneStore:
    def __init__(self, root: Path | None = None) -> None:
        configured = os.getenv("PHONE_AGENT_CONTROL_PLANE_ROOT", "").strip()
        self.root = (
            Path(configured).expanduser()
            if configured
            else root or DEFAULT_CONTROL_PLANE_ROOT
        )
        self.deployments_dir = self.root / "deployments"
        self.active_path = self.root / "active.json"
        self.deployments_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.deployments_dir, 0o700)

    def _path(self, deployment_id: str) -> Path:
        if not re.fullmatch(r"dep_[a-f0-9]{24}", deployment_id):
            raise ControlPlaneError("deployment id is invalid")
        return self.deployments_dir / f"{deployment_id}.json"

    def save(self, record: DeploymentRecord) -> DeploymentRecord:
        atomic_write_private(
            self._path(record.deployment_id),
            json.dumps(record.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        )
        return record

    def stage(
        self,
        package: AgentPackage,
        validation: PackageValidation,
        *,
        base_state_hash: str,
        reason: str,
        actor: str,
    ) -> DeploymentRecord:
        record = DeploymentRecord(
            deployment_id="dep_" + secrets.token_hex(12),
            package_hash=_hash(package),
            base_state_hash=base_state_hash,
            package=package,
            validation=validation,
            reason=reason,
            created_by=actor,
        )
        return self.save(record)

    def load(self, deployment_id: str) -> DeploymentRecord:
        path = self._path(deployment_id)
        harden_private_file(path)
        try:
            return DeploymentRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ControlPlaneError(f"deployment is invalid: {exc}") from exc

    def list(self, limit: int = 50) -> list[DeploymentRecord]:
        records: list[DeploymentRecord] = []
        for path in sorted(self.deployments_dir.glob("dep_*.json"), reverse=True):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                records.append(
                    DeploymentRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
                )
            except (OSError, json.JSONDecodeError, ValueError):
                continue
        return sorted(records, key=lambda item: item.created_at, reverse=True)[:limit]

    def mark_activating(self, deployment_id: str) -> DeploymentRecord:
        record = self.load(deployment_id)
        if record.state != "staged":
            raise ControlPlaneError("only a staged deployment can activate")
        return self.save(record.model_copy(update={"state": "activating"}))

    def mark_failed(self, deployment_id: str, message: str) -> DeploymentRecord:
        record = self.load(deployment_id)
        return self.save(
            record.model_copy(update={"state": "failed", "failure": str(message)[:1_000]})
        )

    def mark_active(self, deployment_id: str) -> DeploymentRecord:
        record = self.load(deployment_id)
        if record.state != "activating":
            raise ControlPlaneError("deployment is not activating")
        for previous in self.list(limit=500):
            if previous.state == "active" and previous.deployment_id != deployment_id:
                self.save(previous.model_copy(update={"state": "superseded"}))
        active = self.save(
            record.model_copy(update={"state": "active", "activated_at": _now()})
        )
        atomic_write_private(
            self.active_path,
            json.dumps(
                {
                    "deployment_id": active.deployment_id,
                    "package_hash": active.package_hash,
                    "activated_at": active.activated_at,
                },
                indent=2,
            )
            + "\n",
        )
        return active

    def active(self) -> DeploymentRecord | None:
        if not self.active_path.exists():
            return None
        harden_private_file(self.active_path)
        try:
            payload = json.loads(self.active_path.read_text(encoding="utf-8"))
            return self.load(str(payload["deployment_id"]))
        except (OSError, KeyError, json.JSONDecodeError, ControlPlaneError):
            return None


def package_hash(package: AgentPackage) -> str:
    return _hash(package)


def state_hash(payload: dict[str, Any]) -> str:
    """Hash effective behavior while ignoring revision counters and fingerprints."""

    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: normalize(item)
                for key, item in sorted(value.items())
                if key
                not in {
                    "revision",
                    "fingerprint",
                    "package_id",
                    "display_name",
                    "version",
                    "created_at",
                    "updated_at",
                }
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    return _hash(normalize(payload))
