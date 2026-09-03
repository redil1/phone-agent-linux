"""Fail unless every dependency finding has a current, exact, owned exception."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast


@dataclass(frozen=True, slots=True)
class Finding:
    package: str
    version: str
    identifiers: frozenset[str]


def _object(path: Path) -> dict[str, Any]:
    payload = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return cast(dict[str, Any], payload)


def _findings(report: dict[str, Any]) -> list[Finding]:
    result: list[Finding] = []
    dependencies = report.get("dependencies")
    if not isinstance(dependencies, list):
        raise ValueError("audit report has no dependencies list")
    for raw_dependency in cast(list[object], dependencies):
        if not isinstance(raw_dependency, dict):
            raise ValueError("audit dependency must be an object")
        dependency = cast(dict[str, Any], raw_dependency)
        vulnerabilities = dependency.get("vulns", [])
        if not isinstance(vulnerabilities, list):
            raise ValueError("audit vulnerabilities must be a list")
        for raw_vulnerability in cast(list[object], vulnerabilities):
            if not isinstance(raw_vulnerability, dict):
                raise ValueError("audit vulnerability must be an object")
            vulnerability = cast(dict[str, Any], raw_vulnerability)
            aliases = vulnerability.get("aliases", [])
            if not isinstance(aliases, list):
                raise ValueError("audit aliases must be a list")
            identifiers = {str(vulnerability.get("id", "")).strip()}
            identifiers.update(str(item).strip() for item in cast(list[object], aliases))
            identifiers.discard("")
            result.append(
                Finding(
                    package=str(dependency.get("name", "")).strip().lower(),
                    version=str(dependency.get("version", "")).strip(),
                    identifiers=frozenset(identifiers),
                )
            )
    return result


def validate(report_path: Path, allowlist_path: Path, *, today: date) -> dict[str, Any]:
    findings = _findings(_object(report_path))
    allowlist = _object(allowlist_path)
    if allowlist.get("version") != 1 or not isinstance(allowlist.get("exceptions"), list):
        raise ValueError("dependency audit allowlist has an invalid schema")

    exceptions = cast(list[object], allowlist["exceptions"])
    used: set[int] = set()
    unexpected: list[str] = []
    for finding in findings:
        match = None
        for index, raw_exception in enumerate(exceptions):
            if not isinstance(raw_exception, dict):
                raise ValueError("dependency audit exception must be an object")
            exception = cast(dict[str, Any], raw_exception)
            exception_ids = {str(exception.get("id", "")).strip()}
            raw_aliases = exception.get("aliases", [])
            if not isinstance(raw_aliases, list):
                raise ValueError("dependency audit exception aliases must be a list")
            exception_ids.update(str(item).strip() for item in cast(list[object], raw_aliases))
            if (
                finding.identifiers & exception_ids
                and finding.package == str(exception.get("package", "")).strip().lower()
                and finding.version == str(exception.get("affected_version", "")).strip()
            ):
                match = (index, exception)
                break
        if match is None:
            unexpected.append(
                f"{finding.package}=={finding.version}: {','.join(sorted(finding.identifiers))}"
            )
            continue
        index, exception = match
        expiry = date.fromisoformat(str(exception.get("expires", "")))
        if expiry < today:
            raise ValueError(f"dependency exception expired: {exception.get('id')}")
        if not str(exception.get("owner", "")).strip() or not str(
            exception.get("justification", "")
        ).strip():
            raise ValueError(f"dependency exception lacks owner or justification: {index}")
        used.add(index)

    if unexpected:
        raise ValueError("unexpected dependency vulnerabilities: " + "; ".join(unexpected))
    unused = set(range(len(exceptions))) - used
    if unused:
        raise ValueError(f"stale dependency exceptions must be removed: {sorted(unused)}")
    return {
        "schema_version": 1,
        "validated_at": datetime.now(UTC).isoformat(),
        "finding_count": len(findings),
        "exception_count": len(used),
        "status": "pass",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("allowlist", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    summary = validate(args.report, args.allowlist, today=date.today())
    rendered = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
