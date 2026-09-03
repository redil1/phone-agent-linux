"""Fail-closed runtime access to PhoneAgent's governed temporary controls."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from importlib import resources
from typing import cast

logger = logging.getLogger("PhoneAgentFeatureFlags")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class FeatureFlagError(ValueError):
    """Raised when a governed rollout control is unknown, invalid, or expired."""


@dataclass(frozen=True, slots=True)
class TemporaryFlag:
    name: str
    owner: str
    default: bool
    expires_on: date
    allowed_values: frozenset[str]
    pipeline_effect: str


@dataclass(frozen=True, slots=True)
class TransitionControl:
    name: str
    owner: str
    default: str
    target_value: str
    expires_on: date
    allowed_values: frozenset[str]
    removal_target: str


def _object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise FeatureFlagError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FeatureFlagError(f"{label} must be a non-empty string")
    return value.strip()


def _date(value: object, *, label: str) -> date:
    try:
        return date.fromisoformat(_string(value, label=label))
    except ValueError as exc:
        raise FeatureFlagError(f"{label} must be an ISO date") from exc


def _strings(value: object, *, label: str) -> frozenset[str]:
    if not isinstance(value, list):
        raise FeatureFlagError(f"{label} must be a list")
    result = frozenset(_string(item, label=label).lower() for item in cast(list[object], value))
    if not result:
        raise FeatureFlagError(f"{label} cannot be empty")
    return result


@lru_cache(maxsize=1)
def _registry() -> tuple[dict[str, TemporaryFlag], dict[str, TransitionControl]]:
    text = (
        resources.files("phone_agent_gateway.ai_bridge")
        .joinpath("feature_flags.json")
        .read_text(encoding="utf-8")
    )
    payload = cast(object, json.loads(text))
    root = _object(payload, label="feature flag registry")
    if root.get("schema_version") != 1:
        raise FeatureFlagError("unsupported feature flag registry schema")

    raw_flags = root.get("temporary_flags")
    raw_controls = root.get("transition_controls")
    if not isinstance(raw_flags, list) or not isinstance(raw_controls, list):
        raise FeatureFlagError("temporary_flags and transition_controls must be lists")

    flags: dict[str, TemporaryFlag] = {}
    for raw in cast(list[object], raw_flags):
        item = _object(raw, label="temporary flag")
        name = _string(item.get("name"), label="temporary flag name")
        default = item.get("default")
        if not isinstance(default, bool):
            raise FeatureFlagError(f"temporary flag default must be boolean: {name}")
        definition = TemporaryFlag(
            name=name,
            owner=_string(item.get("owner"), label=f"{name}.owner"),
            default=default,
            expires_on=_date(item.get("expires_on"), label=f"{name}.expires_on"),
            allowed_values=_strings(item.get("allowed_values"), label=f"{name}.allowed_values"),
            pipeline_effect=_string(item.get("pipeline_effect"), label=f"{name}.pipeline_effect"),
        )
        if name in flags:
            raise FeatureFlagError(f"duplicate temporary flag: {name}")
        flags[name] = definition

    controls: dict[str, TransitionControl] = {}
    for raw in cast(list[object], raw_controls):
        item = _object(raw, label="transition control")
        name = _string(item.get("name"), label="transition control name")
        definition = TransitionControl(
            name=name,
            owner=_string(item.get("owner"), label=f"{name}.owner"),
            default=_string(item.get("default"), label=f"{name}.default").lower(),
            target_value=_string(item.get("target_value"), label=f"{name}.target_value").lower(),
            expires_on=_date(item.get("expires_on"), label=f"{name}.expires_on"),
            allowed_values=_strings(item.get("allowed_values"), label=f"{name}.allowed_values"),
            removal_target=_string(item.get("removal_target"), label=f"{name}.removal_target"),
        )
        if name in controls or name in flags:
            raise FeatureFlagError(f"duplicate governed control: {name}")
        controls[name] = definition
    return flags, controls


def _parse_bool(name: str, raw: str, *, allowed_values: frozenset[str]) -> bool:
    normalized = raw.strip().lower()
    if normalized not in allowed_values:
        raise FeatureFlagError(f"{name} must be one of {sorted(allowed_values)}")
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise FeatureFlagError(f"{name} must be an explicit boolean")


def feature_flag_enabled(
    name: str,
    *,
    default: bool,
    environment: Mapping[str, str] | None = None,
    today: date | None = None,
) -> bool:
    """Resolve one temporary flag and refuse enabled behavior after expiry."""

    flags, _ = _registry()
    definition = flags.get(name)
    if definition is None:
        raise FeatureFlagError(f"unregistered temporary feature flag: {name}")
    if default is not definition.default:
        raise FeatureFlagError(f"source default disagrees with registry for {name}")
    source = os.environ if environment is None else environment
    raw = source.get(name)
    enabled = (
        definition.default
        if raw is None
        else _parse_bool(name, raw, allowed_values=definition.allowed_values)
    )
    if enabled and (today or date.today()) > definition.expires_on:
        raise FeatureFlagError(
            f"{name} expired on {definition.expires_on.isoformat()}; "
            f"owner={definition.owner} must graduate or remove it"
        )
    logger.info(
        "feature_flag_evaluated name=%s enabled=%s owner=%s expires_on=%s pipeline_effect=%s",
        name,
        enabled,
        definition.owner,
        definition.expires_on.isoformat(),
        definition.pipeline_effect,
    )
    return enabled


def transition_control_value(
    name: str,
    *,
    default: str,
    environment: Mapping[str, str] | None = None,
    today: date | None = None,
) -> str:
    """Resolve a migration control and forbid its non-target value after expiry."""

    _, controls = _registry()
    definition = controls.get(name)
    if definition is None:
        raise FeatureFlagError(f"unregistered transition control: {name}")
    normalized_default = default.strip().lower()
    if normalized_default != definition.default:
        raise FeatureFlagError(f"source default disagrees with registry for {name}")
    source = os.environ if environment is None else environment
    value = source.get(name, definition.default).strip().lower()
    if value not in definition.allowed_values:
        raise FeatureFlagError(f"{name} must be one of {sorted(definition.allowed_values)}")
    if value != definition.target_value and (today or date.today()) > definition.expires_on:
        raise FeatureFlagError(
            f"{name}={value} expired on {definition.expires_on.isoformat()}; "
            f"migrate to {definition.target_value} under {definition.removal_target}"
        )
    logger.info(
        "transition_control_evaluated name=%s value=%s target=%s owner=%s expires_on=%s",
        name,
        value,
        definition.target_value,
        definition.owner,
        definition.expires_on.isoformat(),
    )
    return value
