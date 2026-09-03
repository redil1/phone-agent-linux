"""Validate PhoneAgent's pinned dependency and commercial-release policy."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path
from typing import Any, cast

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def _toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as source:
        payload = cast(object, tomllib.load(source))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a TOML table")
    return cast(dict[str, Any], payload)


def _json(path: Path) -> dict[str, Any]:
    payload = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return cast(dict[str, Any], payload)


def _direct_requirements(project: dict[str, Any]) -> list[tuple[str, Requirement]]:
    metadata = cast(dict[str, Any], project.get("project", {}))
    result: list[tuple[str, Requirement]] = []
    sections: list[tuple[str, object]] = [("runtime", metadata.get("dependencies", []))]
    extras = metadata.get("optional-dependencies", {})
    groups = project.get("dependency-groups", {})
    if not isinstance(extras, dict) or not isinstance(groups, dict):
        raise ValueError("optional dependencies and dependency groups must be tables")
    extras_table = cast(dict[str, object], extras)
    groups_table = cast(dict[str, object], groups)
    sections.extend((f"extra:{name}", value) for name, value in extras_table.items())
    sections.extend((f"group:{name}", value) for name, value in groups_table.items())
    for section, raw_requirements in sections:
        if not isinstance(raw_requirements, list):
            raise ValueError(f"{section} dependencies must be a list")
        for raw in cast(list[object], raw_requirements):
            if not isinstance(raw, str):
                raise ValueError(f"{section} dependency must be a string")
            result.append((section, Requirement(raw)))
    return result


def _validate_direct_pins(project: dict[str, Any]) -> int:
    failures: list[str] = []
    requirements = _direct_requirements(project)
    for section, requirement in requirements:
        if requirement.url:
            if not re.search(r"#sha256=[0-9a-f]{64}$", requirement.url):
                failures.append(f"{section}:{requirement.name} URL lacks SHA-256")
        elif not re.fullmatch(r"==[^,]+", str(requirement.specifier)):
            failures.append(f"{section}:{requirement.name} is not exact")
    if failures:
        raise ValueError("unpinned direct dependencies: " + "; ".join(failures))
    return len(requirements)


def _validate_lock(lock: dict[str, Any]) -> tuple[int, set[tuple[str, str]]]:
    if lock.get("requires-python") not in {">=3.11, <3.12", "==3.11.*"}:
        raise ValueError("uv.lock does not enforce the supported Python 3.11 line")
    raw_packages = lock.get("package")
    if not isinstance(raw_packages, list) or not raw_packages:
        raise ValueError("uv.lock has no packages")
    package_entries = cast(list[object], raw_packages)

    packages: set[tuple[str, str]] = set()
    unhashed: list[str] = []
    for raw in package_entries:
        if not isinstance(raw, dict):
            raise ValueError("uv.lock package entries must be tables")
        package = cast(dict[str, Any], raw)
        name = canonicalize_name(str(package.get("name", "")))
        version = str(package.get("version", ""))
        if not name or not version:
            raise ValueError("uv.lock package lacks name or version")
        packages.add((name, version))
        source = package.get("source")
        if not isinstance(source, dict):
            raise ValueError(f"{name}=={version} lacks a locked source")
        source_table = cast(dict[str, object], source)
        if "registry" in source_table or "url" in source_table:
            artifacts: list[dict[str, Any]] = []
            sdist = package.get("sdist")
            wheels = package.get("wheels", [])
            if isinstance(sdist, dict):
                artifacts.append(cast(dict[str, Any], sdist))
            if isinstance(wheels, list):
                artifacts.extend(
                    cast(dict[str, Any], item)
                    for item in cast(list[object], wheels)
                    if isinstance(item, dict)
                )
            if not artifacts or any(
                not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("hash", "")))
                for item in artifacts
            ):
                unhashed.append(f"{name}=={version}")
        if "git" in source_table and not re.search(
            r"#[0-9a-f]{40}$", str(source_table["git"])
        ):
            unhashed.append(f"{name}=={version}")
    if unhashed:
        raise ValueError("lock entries lack immutable artifacts: " + ", ".join(sorted(unhashed)))
    return len(package_entries), packages


def _validate_component_policy(policy: dict[str, Any]) -> tuple[int, int]:
    system_components = policy.get("system_components")
    model_components = policy.get("model_components")
    if not isinstance(system_components, list) or not isinstance(model_components, list):
        raise ValueError("system_components and model_components must be lists")
    system_entries = cast(list[object], system_components)
    model_entries = cast(list[object], model_components)

    for raw in system_entries:
        if not isinstance(raw, dict):
            raise ValueError("system component must be an object")
        item = cast(dict[str, Any], raw)
        required = ("package", "version", "expression", "decision", "owner", "source")
        if not all(str(item.get(field, "")).strip() for field in required):
            raise ValueError("system component lacks exact evidence")
        if "obligations" in str(item["decision"]) and not str(
            item.get("distribution_obligations", "")
        ).strip():
            raise ValueError(f"system component lacks obligations: {item['package']}")

    for raw in model_entries:
        if not isinstance(raw, dict):
            raise ValueError("model component must be an object")
        item = cast(dict[str, Any], raw)
        required = ("model", "revision", "expression", "decision", "owner", "source")
        if not all(str(item.get(field, "")).strip() for field in required):
            raise ValueError("model component lacks exact evidence")
        if not re.fullmatch(r"[0-9a-f]{40}", str(item["revision"])):
            raise ValueError(f"model revision is not immutable: {item['model']}")
        if "obligations" in str(item["decision"]) and not str(
            item.get("distribution_obligations", "")
        ).strip():
            raise ValueError(f"model component lacks obligations: {item['model']}")
    return len(system_entries), len(model_entries)


def _reviewed_family_decisions(
    policy: dict[str, Any],
) -> tuple[int, int, dict[tuple[str, str], tuple[str, str]]]:
    raw_families = policy.get("reviewed_component_families")
    if not isinstance(raw_families, list):
        raise ValueError("reviewed_component_families must be a list")
    families = cast(list[object], raw_families)
    decisions: dict[tuple[str, str], tuple[str, str]] = {}
    for raw in families:
        if not isinstance(raw, dict):
            raise ValueError("reviewed component family must be an object")
        family = cast(dict[str, Any], raw)
        required = ("family", "expression", "decision", "owner", "source")
        if not all(str(family.get(field, "")).strip() for field in required):
            raise ValueError("reviewed component family lacks evidence")
        if "obligations" in str(family["decision"]) and not str(
            family.get("distribution_obligations", "")
        ).strip():
            raise ValueError(f"component family lacks obligations: {family['family']}")
        raw_components = family.get("components")
        if not isinstance(raw_components, dict) or not raw_components:
            raise ValueError(f"component family has no exact members: {family['family']}")
        for name, version in cast(dict[str, object], raw_components).items():
            key = (canonicalize_name(name), str(version))
            if not key[0] or not key[1] or key in decisions:
                raise ValueError(f"invalid or duplicate family component: {name}=={version}")
            decisions[key] = (str(family["decision"]), "release")
    return len(families), len(decisions), decisions


def _validate_license_inventory(
    path: Path,
    decisions: dict[tuple[str, str], tuple[str, str]],
) -> tuple[int, list[str]]:
    payload = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(payload, list):
        raise ValueError("licence inventory must contain a JSON list")
    entries = cast(list[object], payload)
    reviewed_unknowns: list[str] = []
    unreviewed: list[str] = []
    prohibited: list[str] = []
    conditional: list[str] = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise ValueError("licence inventory entry must be an object")
        item = cast(dict[str, Any], raw)
        name = canonicalize_name(str(item.get("Name", "")))
        version = str(item.get("Version", ""))
        expression = str(item.get("License", "")).strip()
        key = (name, version)
        label = f"{name}=={version}"
        decision, scope = decisions.get(key, ("", "release"))
        if expression.upper() == "UNKNOWN":
            if decision and decision != "blocked" and scope == "release":
                reviewed_unknowns.append(label)
            else:
                unreviewed.append(label)
        normalized = expression.upper()
        has_prohibited = "AGPL" in normalized or (
            "GPL" in normalized and "LGPL" not in normalized and "MPL" not in normalized
        )
        if has_prohibited and not decision:
            prohibited.append(f"{label} ({expression})")
        has_conditional = any(
            token in normalized for token in ("LGPL", "PROPRIETARY", "NVIDIA")
        )
        if has_conditional and not decision:
            conditional.append(f"{label} ({expression})")
    if unreviewed:
        raise ValueError("unreviewed UNKNOWN licences: " + ", ".join(sorted(unreviewed)))
    if prohibited:
        raise ValueError("unreviewed prohibited licences: " + ", ".join(sorted(prohibited)))
    if conditional:
        raise ValueError("unreviewed conditional licences: " + ", ".join(sorted(conditional)))
    return len(entries), sorted(reviewed_unknowns)


def validate(
    root: Path,
    policy_path: Path,
    *,
    enforce_release: bool = True,
    licenses_path: Path | None = None,
) -> dict[str, Any]:
    project = _toml(root / "pyproject.toml")
    lock = _toml(root / "uv.lock")
    policy = _json(policy_path)
    if policy.get("schema_version") != 1:
        raise ValueError("dependency policy schema_version must be 1")

    metadata = cast(dict[str, Any], project.get("project", {}))
    licence_policy = policy.get("licence_policy")
    if not isinstance(licence_policy, dict):
        raise ValueError("licence policy is missing")
    licence_table = cast(dict[str, object], licence_policy)
    if metadata.get("license") != licence_table.get("project_expression"):
        raise ValueError("project licence does not match dependency policy")

    direct_count = _validate_direct_pins(project)
    package_count, locked_packages = _validate_lock(lock)
    system_count, model_count = _validate_component_policy(policy)
    family_count, family_component_count, family_decisions = _reviewed_family_decisions(policy)

    reviewed = policy.get("reviewed_components")
    if not isinstance(reviewed, list):
        raise ValueError("reviewed_components must be a list")
    decisions = dict(family_decisions)
    for raw in cast(list[object], reviewed):
        if not isinstance(raw, dict):
            raise ValueError("reviewed component must be an object")
        item = cast(dict[str, Any], raw)
        key = (canonicalize_name(str(item.get("package", ""))), str(item.get("version", "")))
        decision = str(item.get("decision", ""))
        scope = str(item.get("distribution_scope", "release"))
        if not all(str(item.get(field, "")).strip() for field in ("expression", "owner", "source")):
            raise ValueError(f"reviewed component lacks evidence: {key[0]}=={key[1]}")
        if "obligations" in decision and not str(
            item.get("distribution_obligations", "")
        ).strip():
            raise ValueError(f"reviewed component lacks obligations: {key[0]}=={key[1]}")
        if key in decisions:
            raise ValueError(f"duplicate reviewed component: {key[0]}=={key[1]}")
        decisions[key] = (decision, scope)

    blockers = sorted(
        f"{name}=={version}"
        for name, version in locked_packages
        if decisions.get((name, version)) == ("blocked", "release")
    )
    if enforce_release and blockers:
        raise ValueError("commercial release contains blocked licences: " + ", ".join(blockers))

    dockerfile = (root / "Dockerfile.cuda").read_text(encoding="utf-8")
    toolchains = cast(dict[str, Any], policy.get("supported_toolchains", {}))
    python_base = str(toolchains.get("python_base", ""))
    snapshot = str(toolchains.get("debian_snapshot", ""))
    if "pip install" in dockerfile or "FROM nvidia/cuda" in dockerfile:
        raise ValueError("production container bypasses the locked dependency policy")
    if not python_base or f"FROM python@{python_base.split('@')[-1]}" not in dockerfile:
        raise ValueError("production container does not use the governed Python base")
    if (
        not snapshot
        or f"ARG DEBIAN_SNAPSHOT={snapshot}" not in dockerfile
        or 'grep -q "$DEBIAN_SNAPSHOT" /etc/apt/sources.list' not in dockerfile
    ):
        raise ValueError("production container does not use the governed Debian snapshot")
    if 'PHONE_AGENT_STT_PROVIDER="sensevoice"' in dockerfile:
        raise ValueError("commercial image defaults to an evaluation-only STT provider")
    if dockerfile.count("uv sync --frozen --no-dev --extra cloud --extra local") != 2:
        raise ValueError("production container does not consume the frozen deployable graph")
    if "site-packages/nvidia/cublas/lib" not in dockerfile or "nvidia/cudnn/lib" not in dockerfile:
        raise ValueError("native STT cannot discover the governed CUDA wheel libraries")

    inventory_count = 0
    reviewed_unknowns: list[str] = []
    if licenses_path is not None:
        inventory_count, reviewed_unknowns = _validate_license_inventory(
            licenses_path,
            decisions,
        )

    return {
        "schema_version": 1,
        "status": "pass" if not blockers else "blocked",
        "direct_requirement_count": direct_count,
        "locked_package_count": package_count,
        "system_component_count": system_count,
        "model_component_count": model_count,
        "reviewed_family_count": family_count,
        "reviewed_family_component_count": family_component_count,
        "licence_inventory_count": inventory_count,
        "reviewed_unknown_licences": reviewed_unknowns,
        "commercial_release_blockers": blockers,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("security/dependency-policy.json"),
    )
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--licenses", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    summary = validate(
        args.root,
        args.policy,
        enforce_release=not args.report_only,
        licenses_path=args.licenses,
    )
    rendered = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
