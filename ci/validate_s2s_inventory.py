"""Validate the complete, classified Milestone 1 speech-to-speech surface inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, cast

SCHEMA_VERSION = 1
DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "migration" / "s2s-surface-v1.json"
DETECTION = re.compile(
    r"speech[-_ ]?to[-_ ]?speech|\bs2s\b|chatgpt.{0,20}realtime|"
    r"openai.{0,20}realtime|gizmo|webrtc|chatgpt_realtime|openai_realtime|"
    r"voice s2s diagnostic",
    re.IGNORECASE,
)
SKIP_PARTS = frozenset(
    {
        ".git",
        ".venv",
        ".pytest_cache",
        ".ruff_cache",
        ".pyright",
        "__pycache__",
        "artifacts",
        "build",
        "phone_agent_gateway.egg-info",
        "historical_s2s",
    }
)
CONTROL_PATHS = frozenset(
    {
        "ci/validate_s2s_inventory.py",
        "ci/validate_cascade_characterization.py",
        "ci/run-stage.sh",
        "migration/cascade-characterization-v1.json",
        "migration/s2s-surface-v1.json",
        "tests/test_cascade_characterization.py",
        "tests/test_s2s_inventory.py",
        "tests/test_ci_contract.py",
        "docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md",
        "reports/CASCADE_EXECUTION_STATE.md",
        "reports/quality/2026-09-03-m1-01-s2s-inventory.md",
        "reports/quality/2026-09-03-m1-01-evidence.json",
        "skills/phoneagent-cascade-platform/SKILL.md",
    }
)
SKIP_PREFIXES = ("reports/releases/", "reports/quality/2026-09-03-m0-exit-", "reports/quality/2026-09-03-m1-")
ALLOWED_LAYERS = frozenset(
    {
        "runtime",
        "configuration",
        "ui",
        "dependency",
        "test",
        "documentation",
        "architecture",
        "historical_evidence",
        "research",
    }
)
ALLOWED_DISPOSITIONS = frozenset({"delete", "migrate", "rewrite", "retain_historical"})

DELETED_BACKENDS = frozenset(
    {
        "ai_bridge/chatgpt_gizmo_manager.py",
        "ai_bridge/chatgpt_realtime_auth.py",
        "ai_bridge/chatgpt_realtime_pipeline.py",
        "ai_bridge/openai_realtime_websocket_pipeline.py",
    }
)

DELETED_TESTS = frozenset(
    {
        "tests/test_chatgpt_gizmo_manager.py",
        "tests/test_chatgpt_realtime_audio.py",
        "tests/test_chatgpt_realtime_auth.py",
        "tests/test_chatgpt_realtime_pipeline.py",
        "tests/test_openai_realtime_websocket_pipeline.py",
    }
)

DELETED_RUNTIME_BRANCHES = frozenset(
    {
        "PhoneVoiceAgent._await_realtime_preconnect",
        "PhoneVoiceAgent._begin_realtime_preconnect",
        "PhoneVoiceAgent._preload_realtime_pipeline",
    }
)

DELETED_DEPENDENCIES = frozenset(
    {
        "aiortc",
        "curl-cffi",
    }
)

REWRITTEN_DOCS = frozenset(
    {
        "docs/NEW_MAC_INSTALL_GUIDE.md",
        "ai_bridge/phone_voice_agent.py",
    }
)



class InventoryError(ValueError):
    """Raised when an S2S surface is missing, duplicated, or misclassified."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def discover_surface(root: Path) -> set[str]:
    discovered: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink() or any(part in SKIP_PARTS for part in path.parts):
            continue
        relative = path.relative_to(root).as_posix()
        if relative in CONTROL_PATHS or relative.startswith(SKIP_PREFIXES):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if DETECTION.search(relative + "\n" + content):
            discovered.add(relative)
    return discovered


def _load(path: Path) -> dict[str, Any]:
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InventoryError("S2S inventory manifest is unavailable or invalid") from exc
    if not isinstance(raw, dict):
        raise InventoryError("S2S inventory manifest must be an object")
    return cast(dict[str, Any], raw)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InventoryError(f"{label} must be a non-empty string")
    return value.strip()


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise InventoryError(f"{label} must be a list of non-empty strings")
    items = cast(list[object], value)
    if any(not isinstance(item, str) or not item for item in items):
        raise InventoryError(f"{label} must be a list of non-empty strings")
    return cast(list[str], items)


def validate_inventory(root: Path, manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = _load(manifest_path)
    required = {
        "schema_version",
        "inventory_id",
        "captured_at",
        "detection_contract",
        "groups",
        "configuration_keys",
        "dependency_bindings",
        "persisted_surfaces",
        "runtime_branches",
        "event_contracts",
        "shared_behavior_contracts",
        "contains_customer_data",
    }
    if set(manifest) != required or manifest.get("schema_version") != SCHEMA_VERSION:
        raise InventoryError("S2S inventory schema or fields drifted")
    if manifest.get("contains_customer_data") is not False:
        raise InventoryError("S2S inventory must declare contains_customer_data=false")
    detection_contract = manifest.get("detection_contract")
    if not isinstance(detection_contract, dict):
        raise InventoryError("detection_contract must be an object")
    expected_detection = {
        "pattern_sha256": _sha256_bytes(DETECTION.pattern.encode()),
        "control_paths": sorted(CONTROL_PATHS),
    }
    if detection_contract != expected_detection:
        raise InventoryError("S2S detection contract drifted")

    groups_value = manifest.get("groups")
    if not isinstance(groups_value, list) or not groups_value:
        raise InventoryError("S2S inventory groups are missing")
    groups = cast(list[object], groups_value)
    classified: dict[str, str] = {}
    group_counts: Counter[str] = Counter()
    dispositions: Counter[str] = Counter()
    for raw_group_value in groups:
        if not isinstance(raw_group_value, dict):
            raise InventoryError("S2S group fields drifted")
        raw_group = cast(dict[str, object], raw_group_value)
        if set(raw_group) != {
            "id",
            "layer",
            "disposition",
            "target_item",
            "rationale",
            "paths",
        }:
            raise InventoryError("S2S group fields drifted")
        group_id = _string(raw_group["id"], "group.id")
        layer = _string(raw_group["layer"], f"{group_id}.layer")
        disposition = _string(raw_group["disposition"], f"{group_id}.disposition")
        target_item = _string(raw_group["target_item"], f"{group_id}.target_item")
        _string(raw_group["rationale"], f"{group_id}.rationale")
        if layer not in ALLOWED_LAYERS or disposition not in ALLOWED_DISPOSITIONS:
            raise InventoryError(f"{group_id} has an unknown layer or disposition")
        if not re.fullmatch(r"M1-(?:0[2-9]|10)(?:/M1-(?:0[2-9]|10))*", target_item):
            raise InventoryError(f"{group_id} has an invalid migration target")
        paths = _string_list(raw_group["paths"], f"{group_id}.paths")
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise InventoryError(f"{group_id} paths must be sorted and unique")
        for relative in paths:
            if relative in classified:
                raise InventoryError(f"S2S surface is classified twice: {relative}")
            if relative in DELETED_BACKENDS or relative in DELETED_TESTS:
                if (root / relative).exists():
                    raise InventoryError(f"deleted S2S file still exists on disk: {relative}")
            elif not (root / relative).is_file():
                raise InventoryError(f"classified S2S surface is missing: {relative}")
            classified[relative] = group_id
        group_counts[layer] += len(paths)
        dispositions[disposition] += len(paths)

    discovered = discover_surface(root)
    expected_surviving = set(classified) - DELETED_BACKENDS - DELETED_TESTS - REWRITTEN_DOCS
    if discovered != expected_surviving:
        raise InventoryError(
            "S2S surface inventory drifted: "
            f"unclassified={sorted(discovered - expected_surviving)} "
            f"stale={sorted(expected_surviving - discovered)}"
        )

    config_keys = _string_list(manifest.get("configuration_keys"), "configuration_keys")
    if config_keys != sorted(config_keys) or len(config_keys) != len(set(config_keys)):
        raise InventoryError("configuration_keys must be sorted and unique")
    searchable_paths = [
        root / relative
        for relative in classified
        if relative not in DELETED_BACKENDS and relative not in DELETED_TESTS
        and (root / relative).suffix in {".py", ".json", ".html", ".sh", ".example", ""}
    ] + [
        hist_path
        for hist_path in (root / "migration" / "historical_s2s").rglob("*")
        if hist_path.is_file()
        and hist_path.suffix in {".py", ".json", ".html", ".sh", ".example", ""}
    ]
    searchable = "\n".join(
        p.read_text(encoding="utf-8") for p in searchable_paths if p.is_file()
    )
    absent_keys = [key for key in config_keys if key not in searchable]
    if absent_keys:
        raise InventoryError(f"inventoried S2S configuration keys are absent: {absent_keys}")

    bindings_value = manifest.get("dependency_bindings")
    if not isinstance(bindings_value, list) or not bindings_value:
        raise InventoryError("dependency_bindings are missing")
    bindings = cast(list[object], bindings_value)
    binding_names: set[str] = set()
    for raw_value in bindings:
        if not isinstance(raw_value, dict):
            raise InventoryError("dependency binding fields drifted")
        raw = cast(dict[str, object], raw_value)
        if set(raw) != {"package", "usage", "disposition", "target_item"}:
            raise InventoryError("dependency binding fields drifted")
        package = _string(raw["package"], "dependency.package")
        if package in binding_names:
            raise InventoryError(f"duplicate dependency binding: {package}")
        binding_names.add(package)
        _string(raw["usage"], f"{package}.usage")
        _string(raw["disposition"], f"{package}.disposition")
        _string(raw["target_item"], f"{package}.target_item")
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    surviving_bindings = binding_names - DELETED_DEPENDENCIES
    missing_bindings = [name for name in surviving_bindings if f'"{name}==' not in pyproject]
    if missing_bindings:
        raise InventoryError(f"inventoried dependency bindings are absent: {missing_bindings}")
    retained_deleted = [name for name in DELETED_DEPENDENCIES if f'"{name}==' in pyproject]
    if retained_deleted:
        raise InventoryError(f"deleted dependency bindings are still present in pyproject.toml: {retained_deleted}")

    persisted_value = manifest.get("persisted_surfaces")
    if not isinstance(persisted_value, list):
        raise InventoryError("persisted S2S surfaces are incomplete")
    persisted = cast(list[object], persisted_value)
    if len(persisted) < 4:
        raise InventoryError("persisted S2S surfaces are incomplete")
    for index, raw_value in enumerate(persisted):
        if not isinstance(raw_value, dict):
            raise InventoryError(f"persisted_surfaces[{index}] fields drifted")
        raw = cast(dict[str, object], raw_value)
        if set(raw) != {"path", "data", "target_item"}:
            raise InventoryError(f"persisted_surfaces[{index}] fields drifted")
        for field in ("path", "data", "target_item"):
            _string(raw[field], f"persisted_surfaces[{index}].{field}")

    branches = _string_list(manifest.get("runtime_branches"), "runtime_branches")
    if branches != sorted(branches) or len(branches) != len(set(branches)):
        raise InventoryError("runtime_branches must be sorted and unique")
    voice_agent = (root / "ai_bridge" / "phone_voice_agent.py").read_text(encoding="utf-8")
    for anchor in branches:
        method = anchor.rsplit(".", 1)[-1]
        if anchor in DELETED_RUNTIME_BRANCHES:
            if method in voice_agent:
                raise InventoryError(f"deleted S2S runtime branch still present: {anchor}")
        elif method not in voice_agent:
            raise InventoryError(f"inventoried S2S runtime branch is absent: {anchor}")

    contracts_value = manifest.get("event_contracts")
    if not isinstance(contracts_value, list):
        raise InventoryError("event_contracts are missing")
    contracts = cast(list[object], contracts_value)
    event_ids: set[str] = set()
    event_count = 0
    historical_dir = root / "migration" / "historical_s2s"
    event_source = "\n".join(
        (historical_dir / filename).read_text(encoding="utf-8")
        for filename in (
            "chatgpt_realtime_pipeline.py",
            "openai_realtime_websocket_pipeline.py",
        )
    )
    for index, raw_value in enumerate(contracts):
        if not isinstance(raw_value, dict):
            raise InventoryError(f"event_contracts[{index}] fields drifted")
        raw = cast(dict[str, object], raw_value)
        if set(raw) != {"id", "disposition", "target_item", "events"}:
            raise InventoryError(f"event_contracts[{index}] fields drifted")
        contract_id = _string(raw["id"], f"event_contracts[{index}].id")
        if contract_id in event_ids:
            raise InventoryError(f"duplicate event contract: {contract_id}")
        event_ids.add(contract_id)
        _string(raw["disposition"], f"event_contracts[{index}].disposition")
        _string(raw["target_item"], f"event_contracts[{index}].target_item")
        events = _string_list(raw["events"], f"event_contracts[{index}].events")
        if events != sorted(events) or len(events) != len(set(events)):
            raise InventoryError(f"{contract_id} events must be sorted and unique")
        missing_events = [event for event in events if event not in event_source]
        if missing_events:
            raise InventoryError(f"inventoried {contract_id} events are absent: {missing_events}")
        event_count += len(events)
    if event_ids != {"platform-shared", "provider-protocol"}:
        raise InventoryError("event contract classes are incomplete")

    behaviors_value = manifest.get("shared_behavior_contracts")
    if not isinstance(behaviors_value, list):
        raise InventoryError("shared_behavior_contracts are missing")
    behaviors = cast(list[object], behaviors_value)
    required_behaviors = {
        "opening",
        "transcript",
        "tools",
        "interruption",
        "end_call",
        "recovery",
        "evaluation",
        "memory",
        "call_state",
        "verified_playout",
    }
    behavior_ids: set[str] = set()
    for index, raw_value in enumerate(behaviors):
        if not isinstance(raw_value, dict):
            raise InventoryError(f"shared_behavior_contracts[{index}] fields drifted")
        raw = cast(dict[str, object], raw_value)
        if set(raw) != {"id", "source", "cascade_owner", "target_item"}:
            raise InventoryError(f"shared_behavior_contracts[{index}] fields drifted")
        behavior_ids.add(_string(raw["id"], f"shared_behavior_contracts[{index}].id"))
        _string_list(raw["source"], f"shared_behavior_contracts[{index}].source")
        _string(raw["cascade_owner"], f"shared_behavior_contracts[{index}].cascade_owner")
        if raw["target_item"] != "M1-02":
            raise InventoryError("all shared behavior characterization belongs to M1-02")
    if behavior_ids != required_behaviors:
        raise InventoryError(f"shared behavior inventory drifted: {sorted(behavior_ids)}")

    canonical_paths = "\n".join(sorted(classified)) + "\n"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "inventory_id": _string(manifest["inventory_id"], "inventory_id"),
        "surface_count": len(classified),
        "surface_path_sha256": _sha256_bytes(canonical_paths.encode()),
        "group_count": len(groups),
        "layer_counts": dict(sorted(group_counts.items())),
        "disposition_counts": dict(sorted(dispositions.items())),
        "configuration_key_count": len(config_keys),
        "dependency_binding_count": len(binding_names),
        "persisted_surface_count": len(persisted),
        "runtime_branch_count": len(branches),
        "event_contract_count": len(contracts),
        "event_name_count": event_count,
        "shared_behavior_count": len(behavior_ids),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate_inventory(args.root.resolve(), args.manifest.resolve())
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
