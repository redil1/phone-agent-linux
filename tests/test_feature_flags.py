"""Governed rollout controls must fail closed and preserve one Cascade target."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from phone_agent_gateway.ai_bridge.control_plane import RuntimeControl
from phone_agent_gateway.ai_bridge.feature_flags import (
    FeatureFlagError,
    feature_flag_enabled,
    transition_control_value,
)
from phone_agent_gateway.ci.validate_feature_flags import validate

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "ai_bridge" / "feature_flags.json"
REGISTERED_ON = date(2026, 9, 2)


def test_repository_registry_passes_and_has_no_alternate_pipeline_flags() -> None:
    result = validate(ROOT, REGISTRY, today=REGISTERED_ON)

    assert result["status"] == "pass"
    assert result["temporary_flag_count"] == 4
    assert result["alternate_pipeline_flags"] == []


@pytest.mark.parametrize(
    ("name", "default"),
    [
        ("PHONE_AGENT_SPECULATIVE_PIPELINE", False),
        ("PHONE_AGENT_CONVERSATIONAL_REFLEX", False),
        ("PHONE_AGENT_SUPERTONIC_FALLBACK_TO_EDGE", True),
        ("PHONE_AGENT_IDENTITY_PROPOSALS_ENABLED", False),
    ],
)
def test_temporary_flag_defaults_match_registry(name: str, default: bool) -> None:
    assert (
        feature_flag_enabled(name, default=default, environment={}, today=REGISTERED_ON) is default
    )


@pytest.mark.parametrize("enabled_value", ["1", "ON", "true", "Yes"])
def test_documented_boolean_aliases_enable_flag(enabled_value: str) -> None:
    assert feature_flag_enabled(
        "PHONE_AGENT_SPECULATIVE_PIPELINE",
        default=False,
        environment={"PHONE_AGENT_SPECULATIVE_PIPELINE": enabled_value},
        today=REGISTERED_ON,
    )


def test_unknown_invalid_and_default_drift_fail_closed() -> None:
    with pytest.raises(FeatureFlagError, match="unregistered"):
        feature_flag_enabled("PHONE_AGENT_UNKNOWN", default=False, environment={})
    with pytest.raises(FeatureFlagError, match="must be one of"):
        feature_flag_enabled(
            "PHONE_AGENT_SPECULATIVE_PIPELINE",
            default=False,
            environment={"PHONE_AGENT_SPECULATIVE_PIPELINE": "perhaps"},
        )
    with pytest.raises(FeatureFlagError, match="source default disagrees"):
        feature_flag_enabled("PHONE_AGENT_SPECULATIVE_PIPELINE", default=True, environment={})


def test_enabled_expired_flag_fails_but_safe_disabled_value_remains_available() -> None:
    after_expiry = date(2026, 12, 1)
    with pytest.raises(FeatureFlagError, match="expired on 2026-11-30"):
        feature_flag_enabled(
            "PHONE_AGENT_SPECULATIVE_PIPELINE",
            default=False,
            environment={"PHONE_AGENT_SPECULATIVE_PIPELINE": "true"},
            today=after_expiry,
        )
    assert not feature_flag_enabled(
        "PHONE_AGENT_SPECULATIVE_PIPELINE",
        default=False,
        environment={},
        today=after_expiry,
    )


def test_transition_control_defaults_to_cascade_and_s2s_expires() -> None:
    assert (
        transition_control_value(
            "PHONE_AGENT_PIPELINE_MODE", default="cascade", environment={}, today=REGISTERED_ON
        )
        == "cascade"
    )
    assert (
        transition_control_value(
            "PHONE_AGENT_PIPELINE_MODE",
            default="cascade",
            environment={"PHONE_AGENT_PIPELINE_MODE": "s2s_chatgpt_realtime"},
            today=date(2026, 10, 1),
        )
        == "s2s_chatgpt_realtime"
    )
    with pytest.raises(FeatureFlagError, match="migrate to cascade under M1-05"):
        transition_control_value(
            "PHONE_AGENT_PIPELINE_MODE",
            default="cascade",
            environment={"PHONE_AGENT_PIPELINE_MODE": "s2s_chatgpt_realtime"},
            today=date(2026, 10, 2),
        )
    assert (
        transition_control_value(
            "PHONE_AGENT_PIPELINE_MODE",
            default="cascade",
            environment={"PHONE_AGENT_PIPELINE_MODE": "cascade"},
            today=date(2030, 1, 1),
        )
        == "cascade"
    )


def test_validator_rejects_temporary_alternate_pipeline(tmp_path: Path) -> None:
    payload = json.loads(REGISTRY.read_text(encoding="utf-8"))
    payload["temporary_flags"][0]["pipeline_effect"] = "alternate_pipeline"
    mutated = tmp_path / "feature_flags.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="alternate pipeline"):
        validate(ROOT, mutated, today=REGISTERED_ON)


def test_validator_rejects_expired_registry(tmp_path: Path) -> None:
    payload = json.loads(REGISTRY.read_text(encoding="utf-8"))
    payload["temporary_flags"][0]["expires_on"] = "2026-09-01"
    mutated = tmp_path / "feature_flags.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="temporary flag is expired"):
        validate(ROOT, mutated, today=REGISTERED_ON)


def test_validator_rejects_declared_but_unimplemented_telemetry(tmp_path: Path) -> None:
    payload = json.loads(REGISTRY.read_text(encoding="utf-8"))
    payload["temporary_flags"][0]["telemetry"].append("event_that_cannot_be_emitted")
    mutated = tmp_path / "feature_flags.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="telemetry that is not emitted"):
        validate(ROOT, mutated, today=REGISTERED_ON)


def test_control_plane_safe_defaults_are_cascade_without_experiments() -> None:
    control = RuntimeControl()

    assert control.pipeline_mode == "cascade"
    assert control.speculative_pipeline_enabled is False
