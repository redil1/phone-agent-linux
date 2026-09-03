"""The release dependency graph must remain pinned, reviewable, and reproducible."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from phone_agent_gateway.ci.prepare_audit_requirements import prepare
from phone_agent_gateway.ci.validate_dependency_policy import validate

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as source:
        return tomllib.load(source)


def test_python_direct_dependencies_are_exact_and_dev_is_a_group() -> None:
    project = _pyproject()
    metadata = project["project"]
    assert isinstance(metadata, dict)
    assert metadata["license"] == "LicenseRef-Proprietary"
    assert metadata["requires-python"] == ">=3.11,<3.12"

    groups: list[list[str]] = [metadata["dependencies"]]
    extras = metadata["optional-dependencies"]
    assert isinstance(extras, dict)
    assert "dev" not in extras
    groups.extend(extras.values())
    dependency_groups = project["dependency-groups"]
    assert isinstance(dependency_groups, dict)
    groups.extend(dependency_groups.values())

    for dependencies in groups:
        assert isinstance(dependencies, list)
        for raw in dependencies:
            requirement = Requirement(raw)
            if requirement.url:
                assert "#sha256=" in requirement.url, raw
            else:
                assert re.fullmatch(r"==[^,]+", str(requirement.specifier)), raw


def test_lockfiles_and_cuda_sources_are_explicit() -> None:
    project = _pyproject()
    uv = project["tool"]["uv"]
    assert uv["sources"]["torch"] == {"index": "pytorch-cu129"}
    assert uv["index"] == [
        {
            "name": "pytorch-cu129",
            "url": "https://download.pytorch.org/whl/cu129",
            "explicit": True,
        }
    ]

    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'version = "2.13.0+cu129"' in lock
    assert 'name = "cuda-toolkit"\nversion = "12.9.1"' in lock
    assert 'version = "13.0.3.0"' not in lock
    assert 'name = "faster-whisper"' in lock

    cargo = tomllib.loads((ROOT / "whatsapp_channel/rust_caller/Cargo.lock").read_text())
    assert cargo["version"] == 4
    assert any(
        package["name"] == "whatsapp-rust"
        and "rev=6966ddc25d4f896cfefa8fb7aa2025ca824ce37f" in package["source"]
        for package in cargo["package"]
    )


def test_commercial_local_extra_excludes_evaluation_and_in_process_gpl() -> None:
    project = _pyproject()
    extras = project["project"]["optional-dependencies"]
    groups = project["dependency-groups"]
    local = "\n".join(extras["local"]).lower()
    assert all(name not in local for name in ("funasr", "kaldiio", "kokoro", "phonemizer"))
    evaluation = "\n".join(groups["sensevoice-evaluation"]).lower()
    assert "funasr==" in evaluation
    assert "modelscope==" in evaluation


def test_production_container_consumes_only_the_locked_graph() -> None:
    dockerfile = (ROOT / "Dockerfile.cuda").read_text(encoding="utf-8")
    assert "FROM python@sha256:" in dockerfile
    assert "FROM nvidia/cuda" not in dockerfile
    assert "FROM ghcr.io/astral-sh/uv@sha256:" in dockerfile
    assert "ARG DEBIAN_SNAPSHOT=20250721T000000Z" in dockerfile
    assert 'grep -q "$DEBIAN_SNAPSHOT" /etc/apt/sources.list' in dockerfile
    assert "pip install" not in dockerfile
    assert dockerfile.count("uv sync --frozen --no-dev --extra cloud --extra local") == 2
    assert "site-packages/nvidia/cublas/lib" in dockerfile
    assert "site-packages/nvidia/cudnn/lib" in dockerfile
    assert 'CMD ["--host", "127.0.0.1", "--port", "8090"]' in dockerfile


def test_dependency_policy_has_owned_cadence_and_toolchains() -> None:
    policy = json.loads((ROOT / "security/dependency-policy.json").read_text(encoding="utf-8"))
    assert policy["schema_version"] == 1
    assert set(policy["dependency_classes"]) == {
        "python-runtime",
        "python-development",
        "rust-sidecar",
        "android-toolchain",
        "container-base",
        "models",
    }
    assert all(item["owner"] for item in policy["dependency_classes"].values())
    assert policy["update_cadence"]["scheduled_review_days"] <= 30
    assert policy["vulnerability_policy"]["critical_triage_hours"] <= 24
    assert policy["supported_toolchains"]["python"] == ["3.11.13"]
    assert policy["supported_toolchains"]["cuda"] == "12.9"
    assert policy["supported_toolchains"]["android_compile_sdk"] == 34


def test_governance_validator_exposes_current_commercial_blockers() -> None:
    summary = validate(
        ROOT,
        ROOT / "security" / "dependency-policy.json",
        enforce_release=False,
    )
    assert summary["status"] == "pass"
    assert summary["commercial_release_blockers"] == []
    assert validate(ROOT, ROOT / "security" / "dependency-policy.json")["status"] == "pass"


def test_audit_manifest_normalizes_only_the_governed_cuda_build(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    output = tmp_path / "audit.txt"
    report = tmp_path / "normalizations.json"
    source.write_text("torch==2.13.0+cu129\nnltk==3.10.3\n", encoding="utf-8")
    summary = prepare(
        source,
        ROOT / "security" / "dependency-policy.json",
        output,
        report,
    )
    assert output.read_text(encoding="utf-8") == "torch==2.13.0\nnltk==3.10.3\n"
    assert summary["requirement_count"] == 2
    assert json.loads(report.read_text(encoding="utf-8"))["normalizations"] == [
        {
            "advisory_version": "2.13.0",
            "locked_version": "2.13.0+cu129",
            "package": "torch",
        }
    ]


def test_audit_manifest_fails_if_governed_normalization_is_stale(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("torch==2.12.1+cu129\n", encoding="utf-8")
    with pytest.raises(ValueError, match="did not exactly match"):
        prepare(
            source,
            ROOT / "security" / "dependency-policy.json",
            tmp_path / "audit.txt",
            tmp_path / "normalizations.json",
        )


def test_unknown_licence_requires_an_exact_review(tmp_path: Path) -> None:
    inventory = tmp_path / "licenses.json"
    inventory.write_text(
        json.dumps([{"Name": "google-crc32c", "Version": "1.8.0", "License": "UNKNOWN"}]),
        encoding="utf-8",
    )
    summary = validate(
        ROOT,
        ROOT / "security" / "dependency-policy.json",
        licenses_path=inventory,
    )
    assert summary["reviewed_unknown_licences"] == ["google-crc32c==1.8.0"]

    inventory.write_text(
        json.dumps([{"Name": "unreviewed", "Version": "1.0", "License": "UNKNOWN"}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unreviewed UNKNOWN"):
        validate(
            ROOT,
            ROOT / "security" / "dependency-policy.json",
            licenses_path=inventory,
        )
