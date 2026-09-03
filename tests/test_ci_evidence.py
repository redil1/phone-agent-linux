"""Machine-readable CI evidence must fail closed when its inputs drift."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from phone_agent_gateway.ci.validate_dependency_audit import validate as validate_audit
from phone_agent_gateway.ci.validate_sbom import validate as validate_sbom


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_dependency_audit_requires_an_exact_current_exception(tmp_path: Path) -> None:
    report = _write(
        tmp_path / "audit.json",
        {
            "dependencies": [
                {
                    "name": "nltk",
                    "version": "3.10.3",
                    "vulns": [{"id": "PYSEC-2026-3740", "aliases": ["CVE-2026-81726"]}],
                }
            ]
        },
    )
    allowlist = _write(
        tmp_path / "allowlist.json",
        {
            "version": 1,
            "exceptions": [
                {
                    "id": "PYSEC-2026-3740",
                    "aliases": ["CVE-2026-81726"],
                    "package": "nltk",
                    "affected_version": "3.10.3",
                    "owner": "dependency-governance",
                    "expires": "2026-12-01",
                    "justification": "No fixed release and the vulnerable path is unreachable.",
                }
            ],
        },
    )

    summary = validate_audit(report, allowlist, today=date(2026, 9, 2))
    assert summary["status"] == "pass"
    with pytest.raises(ValueError, match="expired"):
        validate_audit(report, allowlist, today=date(2026, 12, 2))


def test_dependency_audit_rejects_unexpected_findings(tmp_path: Path) -> None:
    report = _write(
        tmp_path / "audit.json",
        {
            "dependencies": [
                {"name": "example", "version": "1.0", "vulns": [{"id": "CVE-new"}]}
            ]
        },
    )
    allowlist = _write(tmp_path / "allowlist.json", {"version": 1, "exceptions": []})
    with pytest.raises(ValueError, match="unexpected dependency vulnerabilities"):
        validate_audit(report, allowlist, today=date(2026, 9, 2))


def test_sbom_requires_complete_components_and_graph(tmp_path: Path) -> None:
    valid = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "components": [
            {
                "name": "example",
                "version": "1.0",
                "bom-ref": "example@1.0",
                "purl": "pkg:pypi/example@1.0",
            }
        ],
        "dependencies": [{"ref": "example@1.0", "dependsOn": []}],
    }
    path = _write(tmp_path / "sbom.json", valid)
    assert validate_sbom(path)["component_count"] == 1

    valid["components"][0].pop("purl")
    _write(path, valid)
    with pytest.raises(ValueError, match="lack purl"):
        validate_sbom(path)
