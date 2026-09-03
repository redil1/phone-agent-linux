"""Fail closed unless every shared behavior has executable Cascade characterization."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
from typing import Any, cast

SCHEMA_VERSION = 1
EXPECTED_IDS = frozenset(
    {
        "call_state",
        "end_call",
        "evaluation",
        "interruption",
        "memory",
        "opening",
        "recovery",
        "tools",
        "transcript",
        "verified_playout",
    }
)
DEFAULT_MATRIX = Path(__file__).resolve().parents[1] / "migration" / "cascade-characterization-v1.json"
DEFAULT_INVENTORY = Path(__file__).resolve().parents[1] / "migration" / "s2s-surface-v1.json"


class CharacterizationError(ValueError):
    """The deletion prerequisite is incomplete, stale, or not Cascade-only."""


def _load(path: Path) -> dict[str, Any]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CharacterizationError(f"invalid JSON document: {path}") from exc
    if not isinstance(value, dict):
        raise CharacterizationError(f"document must be an object: {path}")
    return cast(dict[str, Any], value)


def _strings(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise CharacterizationError(f"{label} must be a non-empty list")
    items = cast(list[object], value)
    if any(not isinstance(item, str) or not item.strip() for item in items):
        raise CharacterizationError(f"{label} must contain non-empty strings")
    result = cast(list[str], items)
    if len(result) != len(set(result)):
        raise CharacterizationError(f"{label} must be unique")
    return result


def _test_functions(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise CharacterizationError(f"cannot parse characterized test: {path}") from exc
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    }


def validate_characterization(
    root: Path,
    matrix_path: Path = DEFAULT_MATRIX,
    inventory_path: Path = DEFAULT_INVENTORY,
) -> dict[str, Any]:
    matrix = _load(matrix_path)
    if set(matrix) != {
        "schema_version",
        "suite_id",
        "pipeline",
        "source_inventory_id",
        "contains_customer_data",
        "contracts",
    }:
        raise CharacterizationError("characterization matrix fields drifted")
    if matrix.get("schema_version") != SCHEMA_VERSION or matrix.get("pipeline") != "cascade":
        raise CharacterizationError("characterization matrix must be schema-v1 Cascade")
    if matrix.get("contains_customer_data") is not False:
        raise CharacterizationError("characterization matrix must contain no customer data")

    inventory = _load(inventory_path)
    if matrix.get("source_inventory_id") != inventory.get("inventory_id"):
        raise CharacterizationError("characterization does not name the active S2S inventory")
    inventoried_ids = {
        str(item.get("id"))
        for item in cast(list[dict[str, Any]], inventory.get("shared_behavior_contracts", []))
    }
    if inventoried_ids != set(EXPECTED_IDS):
        raise CharacterizationError("active inventory shared behaviors drifted")

    contracts_value = matrix.get("contracts")
    if not isinstance(contracts_value, list):
        raise CharacterizationError("contracts must be a list")
    contracts = cast(list[object], contracts_value)
    ids: set[str] = set()
    all_nodeids: set[str] = set()
    for value in contracts:
        if not isinstance(value, dict):
            raise CharacterizationError("each contract must be an object")
        contract = cast(dict[str, object], value)
        if set(contract) != {
            "id",
            "owner_modules",
            "guarantees",
            "success_nodeids",
            "failure_nodeids",
        }:
            raise CharacterizationError("characterization contract fields drifted")
        contract_id = str(contract["id"])
        if contract_id in ids:
            raise CharacterizationError(f"duplicate behavior contract: {contract_id}")
        ids.add(contract_id)
        owners = _strings(contract["owner_modules"], f"{contract_id}.owner_modules")
        _strings(contract["guarantees"], f"{contract_id}.guarantees")
        nodeids = _strings(contract["success_nodeids"], f"{contract_id}.success_nodeids")
        nodeids += _strings(contract["failure_nodeids"], f"{contract_id}.failure_nodeids")
        for relative in owners:
            if not (root / relative).is_file():
                raise CharacterizationError(f"missing Cascade owner: {relative}")
        for nodeid in nodeids:
            parts = nodeid.split("::")
            if len(parts) != 2 or not parts[0].startswith("tests/"):
                raise CharacterizationError(f"invalid pytest node id: {nodeid}")
            relative, function = parts
            lowered = relative.lower()
            if "s2s" in lowered or "realtime" in lowered:
                raise CharacterizationError(f"legacy pipeline test cannot prove Cascade: {nodeid}")
            path = root / relative
            if not path.is_file() or function not in _test_functions(path):
                raise CharacterizationError(f"characterized test is missing: {nodeid}")
            all_nodeids.add(nodeid)
    if ids != set(EXPECTED_IDS):
        raise CharacterizationError(
            f"behavior coverage drifted: missing={sorted(EXPECTED_IDS - ids)} "
            f"extra={sorted(ids - EXPECTED_IDS)}"
        )
    payload = matrix_path.read_bytes()
    return {
        "status": "pass",
        "schema_version": SCHEMA_VERSION,
        "suite_id": matrix["suite_id"],
        "pipeline": "cascade",
        "behavior_count": len(ids),
        "test_node_count": len(all_nodeids),
        "matrix_sha256": hashlib.sha256(payload).hexdigest(),
        "contains_customer_data": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate_characterization(args.root.resolve(), args.matrix, args.inventory)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
