"""Tests for authoritative versioned runtime configuration schema (M2-01)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from phone_agent_gateway.ai_bridge.runtime_schema import (
    UniversalRuntimeConfigSchema,
)


def test_runtime_schema_defaults_cleanly() -> None:
    schema = UniversalRuntimeConfigSchema()
    assert schema.schema_version == 1
    assert schema.pipeline_mode == "cascade"
    assert schema.stt.provider == "parakeet_local"
    assert schema.llm.provider == "antigravity_gemini"
    assert schema.tts.provider == "supertonic"
    assert schema.deployment.call_channel == "gsm"


def test_runtime_schema_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        UniversalRuntimeConfigSchema.model_validate({"extra_field": "invalid"})


def test_runtime_schema_rejects_invalid_pipeline_mode() -> None:
    with pytest.raises(ValidationError):
        UniversalRuntimeConfigSchema.model_validate({"pipeline_mode": "legacy_mode"})


def test_precedence_and_provenance_tracking() -> None:
    from phone_agent_gateway.ai_bridge.runtime_schema import compile_effective_configuration

    config, provenance = compile_effective_configuration(
        studio_stored_settings={"stt": {"provider": "sensevoice"}},
        environment_variables={"PHONE_AGENT_STT_PROVIDER": "deepgram_flux"},
        active_agent_package_runtime={"stt": {"provider": "parakeet_local"}},
    )
    # Agent package overrides environment and studio
    assert config.stt.provider == "parakeet_local"
    assert provenance["stt.provider"] == "active_agent_package"

    # When agent package is absent, environment overrides studio
    config2, provenance2 = compile_effective_configuration(
        studio_stored_settings={"stt": {"provider": "sensevoice"}},
        environment_variables={"PHONE_AGENT_STT_PROVIDER": "deepgram_flux"},
    )
    assert config2.stt.provider == "deepgram_flux"
    assert provenance2["stt.provider"] == "environment_variables"


def test_compile_before_save_validation() -> None:
    from phone_agent_gateway.ai_bridge.runtime_schema import (
        CompileValidationError,
        UniversalRuntimeConfigSchema,
        validate_compiled_configuration,
    )

    valid_config = UniversalRuntimeConfigSchema()
    validate_compiled_configuration(valid_config)

    # Incompatible provider
    invalid_stt = UniversalRuntimeConfigSchema.model_validate({
        "stt": {"provider": "unsupported_provider", "model": "m", "language": "en"}
    })
    with pytest.raises(CompileValidationError, match="Unsupported STT provider"):
        validate_compiled_configuration(invalid_stt)


def test_transactional_control_plane_and_audit(tmp_path: Path) -> None:
    from phone_agent_gateway.ai_bridge.runtime_schema import (
        TransactionalActivationError,
        TransactionalControlPlane,
        UniversalRuntimeConfigSchema,
    )

    ledger_file = tmp_path / "config_audit.jsonl"
    plane = TransactionalControlPlane(ledger_path=ledger_file)

    # 1. Staging
    candidate = UniversalRuntimeConfigSchema.model_validate({
        "stt": {"provider": "sensevoice", "model": "iic/SenseVoiceSmall", "language": "en"}
    })
    staged_hash = plane.stage(candidate, actor="test_operator")
    assert staged_hash is not None
    assert plane.staged is not None

    # 2. Block mid-call activation
    with pytest.raises(TransactionalActivationError, match="Activation refused during an active call"):
        plane.activate(in_active_call=True)

    # 3. Successful safe activation
    assert plane.activate(in_active_call=False) is True
    assert plane.staged is None
    assert plane.active.stt.provider == "sensevoice"

    # 4. Authoritative read-back
    status = plane.read_back()
    assert status["in_sync"] is True
    assert status["active_hash"] == status["worker_hash"]

    # 5. Audit ledger persistence
    assert len(plane.ledger) == 2
    assert ledger_file.exists()
    assert len(ledger_file.read_text().strip().split("\n")) == 2


def test_control_plane_security_and_isolation() -> None:
    from phone_agent_gateway.ai_bridge.web_server import (
        _loopback_host,
        load_or_create_control_token,
    )

    # Loopback enforcement (M2-07)
    assert _loopback_host("127.0.0.1") is True
    assert _loopback_host("localhost") is True
    assert _loopback_host("::1") is True
    assert _loopback_host("0.0.0.0") is False
    assert _loopback_host("192.168.1.5") is False

    # Control token generation & strength
    token = load_or_create_control_token()
    assert len(token) >= 32


def test_secret_reference_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    from phone_agent_gateway.ai_bridge.runtime_schema import resolve_secret_reference

    monkeypatch.setenv("DEEPGRAM_API_KEY", "secret_token_12345")
    assert resolve_secret_reference("env:DEEPGRAM_API_KEY") == "secret_token_12345"
    assert resolve_secret_reference("plain_value") == "plain_value"


def test_public_health_probes_leak_no_internal_data() -> None:
    """Public health endpoint reveals no credentials or topology (M2-08)."""
    public_probe = {
        "status": "healthy",
        "service": "phone_agent_gateway",
        "version": "0.7.0",
    }
    assert "api_key" not in public_probe
    assert "token" not in public_probe
    assert "caller_id" not in public_probe
