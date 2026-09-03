"""Validate temporary rollout controls and the one-Cascade-pipeline invariant."""

from __future__ import annotations

import argparse
import ast
import json
import re
from datetime import date
from pathlib import Path
from typing import cast

_NAME_RE = re.compile(r"^PHONE_AGENT_[A-Z0-9_]+$")
_MILESTONE_RE = re.compile(r"^M\d+-\d+$")
_BOOLEAN_HELPERS = frozenset({"_env_bool", "_environment_bool", "_env_flag"})
_ALLOWED_PIPELINE_EFFECTS = frozenset(
    {
        "same_cascade_graph_only",
        "cascade_provider_fallback_only",
        "agent_state_only",
    }
)
_BOOLEAN_VALUES = frozenset({"0", "1", "false", "no", "off", "on", "true", "yes"})


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return cast(list[object], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _strings(value: object, label: str) -> list[str]:
    result = [_string(item, label) for item in _list(value, label)]
    if not result or len(result) != len(set(result)):
        raise ValueError(f"{label} must contain unique values")
    return result


def _date(value: object, label: str) -> date:
    try:
        return date.fromisoformat(_string(value, label))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date") from exc


def _bindings(root: Path, item: dict[str, object], name: str) -> list[str]:
    bindings = _strings(item.get("bindings"), f"{name}.bindings")
    for binding in bindings:
        path = root / binding
        if not path.is_file():
            raise ValueError(f"{name} binding does not exist: {binding}")
        if name not in path.read_text(encoding="utf-8"):
            raise ValueError(f"{name} is absent from declared binding: {binding}")
    return bindings


def _telemetry_sources(root: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in (root / "ai_bridge").rglob("*.py")
        if "_vendor" not in path.parts
    )


def _rollout(item: dict[str, object], name: str) -> None:
    rollout = _object(item.get("rollout_plan"), f"{name}.rollout_plan")
    stages = _strings(rollout.get("stages"), f"{name}.rollout_plan.stages")
    current = _string(rollout.get("current_stage"), f"{name}.rollout_plan.current_stage")
    if current not in stages:
        raise ValueError(f"{name} current rollout stage is not registered")
    _string(rollout.get("success_criteria"), f"{name}.rollout_plan.success_criteria")
    _string(rollout.get("abort_criteria"), f"{name}.rollout_plan.abort_criteria")


def _rollback(item: dict[str, object], name: str, allowed: set[str]) -> None:
    rollback = _object(item.get("rollback"), f"{name}.rollback")
    safe_value = _string(rollback.get("safe_value"), f"{name}.rollback.safe_value").lower()
    if safe_value not in allowed:
        raise ValueError(f"{name} rollback value is not allowed")
    _string(rollback.get("procedure"), f"{name}.rollback.procedure")


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _discover_python_controls(root: Path) -> tuple[set[str], set[str], set[str]]:
    booleans: set[str] = set()
    temporary: set[str] = set()
    transitions: set[str] = set()
    for path in (root / "ai_bridge").rglob("*.py"):
        if "_vendor" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            function = _call_name(node)
            first = node.args[0]
            if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                continue
            name = first.value
            if function in _BOOLEAN_HELPERS and name.startswith("PHONE_AGENT_"):
                booleans.add(name)
            elif function == "feature_flag_enabled":
                temporary.add(name)
            elif function == "transition_control_value":
                transitions.add(name)
    return booleans, temporary, transitions


def validate(root: Path, registry_path: Path, *, today: date) -> dict[str, object]:
    payload = cast(object, json.loads(registry_path.read_text(encoding="utf-8")))
    registry = _object(payload, "feature flag registry")
    if registry.get("schema_version") != 1:
        raise ValueError("feature flag registry schema_version must be 1")
    _string(registry.get("policy_owner"), "policy_owner")
    maximum_lifetime = registry.get("maximum_lifetime_days")
    if not isinstance(maximum_lifetime, int) or not 1 <= maximum_lifetime <= 180:
        raise ValueError("maximum_lifetime_days must be between 1 and 180")
    telemetry_sources = _telemetry_sources(root)

    temporary_names: set[str] = set()
    for raw in _list(registry.get("temporary_flags"), "temporary_flags"):
        item = _object(raw, "temporary flag")
        name = _string(item.get("name"), "temporary flag name")
        if not _NAME_RE.fullmatch(name) or name in temporary_names:
            raise ValueError(f"invalid or duplicate temporary flag: {name}")
        temporary_names.add(name)
        _string(item.get("owner"), f"{name}.owner")
        _string(item.get("purpose"), f"{name}.purpose")
        registered = _date(item.get("registered_on"), f"{name}.registered_on")
        expires = _date(item.get("expires_on"), f"{name}.expires_on")
        if expires < today:
            raise ValueError(f"temporary flag is expired: {name} ({expires.isoformat()})")
        if not 1 <= (expires - registered).days <= maximum_lifetime:
            raise ValueError(f"temporary flag lifetime exceeds policy: {name}")
        allowed = {value.lower() for value in _strings(item.get("allowed_values"), name)}
        if allowed != set(_BOOLEAN_VALUES) or not isinstance(item.get("default"), bool):
            raise ValueError(f"temporary flag must be boolean: {name}")
        effect = _string(item.get("pipeline_effect"), f"{name}.pipeline_effect")
        if effect not in _ALLOWED_PIPELINE_EFFECTS:
            raise ValueError(f"temporary flag can create an alternate pipeline: {name}")
        _rollout(item, name)
        telemetry = _strings(item.get("telemetry"), f"{name}.telemetry")
        if "feature_flag_evaluated" not in telemetry:
            raise ValueError(f"temporary flag lacks evaluation telemetry: {name}")
        missing_telemetry = [event for event in telemetry if event not in telemetry_sources]
        if missing_telemetry:
            raise ValueError(
                f"temporary flag declares telemetry that is not emitted: {name}: "
                + ", ".join(missing_telemetry)
            )
        _rollback(item, name, allowed)
        if not _MILESTONE_RE.fullmatch(_string(item.get("removal_target"), name)):
            raise ValueError(f"temporary flag lacks a backlog removal target: {name}")
        _bindings(root, item, name)

    transition_names: set[str] = set()
    for raw in _list(registry.get("transition_controls"), "transition_controls"):
        item = _object(raw, "transition control")
        name = _string(item.get("name"), "transition control name")
        if not _NAME_RE.fullmatch(name) or name in transition_names | temporary_names:
            raise ValueError(f"invalid or duplicate transition control: {name}")
        transition_names.add(name)
        _string(item.get("owner"), f"{name}.owner")
        _string(item.get("purpose"), f"{name}.purpose")
        registered = _date(item.get("registered_on"), f"{name}.registered_on")
        expires = _date(item.get("expires_on"), f"{name}.expires_on")
        if expires < today or not 1 <= (expires - registered).days <= maximum_lifetime:
            raise ValueError(f"transition control is expired or overlong: {name}")
        allowed = {value.lower() for value in _strings(item.get("allowed_values"), name)}
        target = _string(item.get("target_value"), f"{name}.target_value").lower()
        default = _string(item.get("default"), f"{name}.default").lower()
        if target != "cascade" or default != target or target not in allowed:
            raise ValueError(f"transition control does not fail toward Cascade: {name}")
        if item.get("creates_alternate_pipeline") is not True:
            raise ValueError(f"transition pipeline debt must be explicit: {name}")
        removal = _string(item.get("removal_target"), f"{name}.removal_target")
        if not re.fullmatch(r"M1-\d+", removal):
            raise ValueError(f"alternate-pipeline transition must be removed in M1: {name}")
        telemetry = _strings(item.get("telemetry"), f"{name}.telemetry")
        if "transition_control_evaluated" not in telemetry:
            raise ValueError(f"transition control lacks evaluation telemetry: {name}")
        missing_telemetry = [event for event in telemetry if event not in telemetry_sources]
        if missing_telemetry:
            raise ValueError(
                f"transition control declares telemetry that is not emitted: {name}: "
                + ", ".join(missing_telemetry)
            )
        _rollback(item, name, allowed)
        _bindings(root, item, name)

    durable_names: set[str] = set()
    for raw in _list(registry.get("durable_controls"), "durable_controls"):
        item = _object(raw, "durable control")
        name = _string(item.get("name"), "durable control name")
        if (
            not _NAME_RE.fullmatch(name)
            or name in durable_names | transition_names | temporary_names
        ):
            raise ValueError(f"invalid or duplicate durable control: {name}")
        durable_names.add(name)
        _string(item.get("owner"), f"{name}.owner")
        _string(item.get("classification"), f"{name}.classification")
        _string(item.get("reason"), f"{name}.reason")
        _bindings(root, item, name)

    discovered_booleans, discovered_temporary, discovered_transitions = _discover_python_controls(
        root
    )
    all_registered = temporary_names | transition_names | durable_names
    unregistered = sorted(discovered_booleans - all_registered)
    if unregistered:
        raise ValueError("unregistered boolean environment controls: " + ", ".join(unregistered))
    if discovered_temporary != temporary_names:
        raise ValueError(
            "temporary flag runtime bindings drifted: "
            f"registered={sorted(temporary_names)} discovered={sorted(discovered_temporary)}"
        )
    if discovered_transitions != transition_names:
        raise ValueError(
            "transition control runtime bindings drifted: "
            f"registered={sorted(transition_names)} discovered={sorted(discovered_transitions)}"
        )

    return {
        "schema_version": 1,
        "status": "pass",
        "validated_on": today.isoformat(),
        "temporary_flag_count": len(temporary_names),
        "transition_control_count": len(transition_names),
        "durable_control_count": len(durable_names),
        "discovered_boolean_control_count": len(discovered_booleans),
        "alternate_pipeline_flags": [],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--registry", type=Path, default=Path("ai_bridge/feature_flags.json"))
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    summary = validate(args.root, args.registry, today=args.today)
    rendered = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
