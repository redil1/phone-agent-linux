"""Create a policy-controlled advisory manifest from the immutable release lock."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def prepare(source: Path, policy_path: Path, output: Path, report: Path) -> dict[str, object]:
    policy_payload = cast(object, json.loads(policy_path.read_text(encoding="utf-8")))
    if not isinstance(policy_payload, dict):
        raise ValueError("dependency policy must be an object")
    policy = cast(dict[str, Any], policy_payload)
    vulnerability_policy = policy.get("vulnerability_policy")
    if not isinstance(vulnerability_policy, dict):
        raise ValueError("vulnerability policy is missing")
    raw_normalizations = cast(dict[str, object], vulnerability_policy).get(
        "audit_version_normalizations"
    )
    if not isinstance(raw_normalizations, dict):
        raise ValueError("audit_version_normalizations must be an object")

    normalizations: dict[tuple[str, str], str] = {}
    for raw_name, raw_rule in cast(dict[str, object], raw_normalizations).items():
        if not isinstance(raw_rule, dict):
            raise ValueError(f"audit normalization must be an object: {raw_name}")
        rule = cast(dict[str, object], raw_rule)
        locked = str(rule.get("locked_version", ""))
        advisory = str(rule.get("advisory_version", ""))
        reason = str(rule.get("reason", ""))
        if not locked or not advisory or not reason:
            raise ValueError(f"audit normalization lacks evidence: {raw_name}")
        normalizations[(canonicalize_name(raw_name), locked)] = advisory

    rendered: list[str] = []
    applied: list[dict[str, str]] = []
    for raw_line in source.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.url or len(requirement.specifier) != 1:
            raise ValueError(f"audit requirement is not an exact registry version: {line}")
        specifier = next(iter(requirement.specifier))
        if specifier.operator != "==":
            raise ValueError(f"audit requirement is not exact: {line}")
        name = canonicalize_name(requirement.name)
        locked_version = specifier.version
        advisory_version = normalizations.get((name, locked_version), locked_version)
        rendered.append(f"{name}=={advisory_version}")
        if advisory_version != locked_version:
            applied.append(
                {
                    "package": name,
                    "locked_version": locked_version,
                    "advisory_version": advisory_version,
                }
            )

    expected = sorted(
        (name, locked, advisory) for (name, locked), advisory in normalizations.items()
    )
    actual = sorted(
        (item["package"], item["locked_version"], item["advisory_version"])
        for item in applied
    )
    if actual != expected:
        raise ValueError("configured audit normalizations did not exactly match the release graph")
    if len(rendered) != len(set(rendered)):
        raise ValueError("audit manifest contains duplicate requirements")

    output.write_text("\n".join(rendered) + "\n", encoding="utf-8")
    summary: dict[str, object] = {
        "schema_version": 1,
        "requirement_count": len(rendered),
        "normalizations": applied,
    }
    report.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("policy", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.policy, args.output, args.report), sort_keys=True))


if __name__ == "__main__":
    main()
