"""Release evidence must be complete, immutable, redacted, and profile-qualified."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from phone_agent_gateway.release.evidence import (
    CATEGORIES,
    CHECKSUM_NAME,
    MANIFEST_NAME,
    ReleaseEvidenceError,
    seal_bundle,
    validate_bundle,
)

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "release" / "evidence-policy.json"
SCHEMA = ROOT / "release" / "evidence.schema.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_manifest(root: Path, manifest: dict[str, Any]) -> None:
    rendered = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    (root / MANIFEST_NAME).write_bytes(rendered)
    (root / CHECKSUM_NAME).write_text(_sha(rendered) + "\n", encoding="ascii")


def _bundle(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / "bundle"
    root.mkdir(parents=True)
    gates: list[dict[str, Any]] = []
    for category in sorted(CATEGORIES):
        required = category in {"security", "test"}
        gate: dict[str, Any] = {
            "gate_id": f"reference-{category}",
            "category": category,
            "required": required,
            "status": "pass" if required else "not_applicable",
            "started_at": "2026-09-03T05:00:00Z",
            "finished_at": "2026-09-03T05:01:00Z",
            "executor": "pytest-fixture",
            "summary": f"Reference {category} result.",
            "metrics": {},
            "artifacts": [],
        }
        if required:
            payload = json.dumps({"category": category, "status": "pass"}).encode()
            path = root / "evidence" / category / "result.json"
            path.parent.mkdir(parents=True)
            path.write_bytes(payload)
            gate["artifacts"] = [
                {
                    "path": f"evidence/{category}/result.json",
                    "sha256": _sha(payload),
                    "bytes": len(payload),
                    "media_type": "application/json",
                    "contains_customer_data": False,
                }
            ]
        else:
            gate["not_applicable_reason"] = (
                "No deployable artifact changed in this development run."
            )
        gates.append(gate)

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "release": {
            "release_id": "m0-09-test",
            "product": "phone-agent-gateway",
            "version": "0.7.0-dev",
            "profile": "development",
            "source_revision": "uncommitted",
            "source_tree_sha256": "a" * 64,
            "created_at": "2026-09-03T05:02:00Z",
        },
        "environment": {
            "profile_id": "linux-test",
            "os": "linux",
            "architecture": "x86_64",
            "python": "3.11",
            "gpu": None,
            "device_profile": None,
        },
        "subjects": [
            {"kind": "source", "identifier": "working-tree", "digest": "sha256:" + "a" * 64}
        ],
        "gates": gates,
        "attestation": {
            "generated_by": "test-suite",
            "generator_version": "release-evidence-v1",
            "generated_at": "2026-09-03T05:02:00Z",
            "command": "pytest tests/test_release_evidence.py",
            "contains_secrets": False,
            "contains_customer_data": False,
        },
    }
    _write_manifest(root, manifest)
    return root, manifest


def test_reference_bundle_is_complete_and_immutable(tmp_path: Path) -> None:
    root, _ = _bundle(tmp_path)

    result = validate_bundle(root, policy_path=POLICY)

    assert result["status"] == "pass"
    assert result["gate_count"] == 8
    assert result["passed_gate_count"] == 2
    assert result["not_applicable_gate_count"] == 6
    assert result["artifact_count"] == 2


def test_seal_recreates_the_exact_manifest_checksum(tmp_path: Path) -> None:
    root, _ = _bundle(tmp_path)
    (root / CHECKSUM_NAME).write_text("0" * 64 + "\n", encoding="ascii")

    digest = seal_bundle(root)

    assert (root / CHECKSUM_NAME).read_text(encoding="ascii").strip() == digest
    assert validate_bundle(root, policy_path=POLICY)["manifest_sha256"] == digest


def test_missing_category_and_failed_required_gate_are_rejected(tmp_path: Path) -> None:
    root, manifest = _bundle(tmp_path)
    manifest["gates"].pop()
    _write_manifest(root, manifest)
    with pytest.raises(ReleaseEvidenceError, match="exactly one gate"):
        validate_bundle(root, policy_path=POLICY)

    root, manifest = _bundle(tmp_path / "second")
    test_gate = next(gate for gate in manifest["gates"] if gate["category"] == "test")
    test_gate["status"] = "fail"
    _write_manifest(root, manifest)
    with pytest.raises(ReleaseEvidenceError, match="failed gate"):
        validate_bundle(root, policy_path=POLICY)


def test_required_state_and_not_applicable_rationale_are_policy_enforced(tmp_path: Path) -> None:
    root, manifest = _bundle(tmp_path)
    security = next(gate for gate in manifest["gates"] if gate["category"] == "security")
    security["required"] = False
    _write_manifest(root, manifest)
    with pytest.raises(ReleaseEvidenceError, match="required state disagrees"):
        validate_bundle(root, policy_path=POLICY)

    root, manifest = _bundle(tmp_path / "second")
    benchmark = next(gate for gate in manifest["gates"] if gate["category"] == "benchmark")
    benchmark["not_applicable_reason"] = "later"
    _write_manifest(root, manifest)
    with pytest.raises(ReleaseEvidenceError, match="owned rationale"):
        validate_bundle(root, policy_path=POLICY)


def test_tampered_and_unlisted_artifacts_are_rejected(tmp_path: Path) -> None:
    root, _ = _bundle(tmp_path)
    (root / "evidence" / "test" / "result.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(ReleaseEvidenceError, match="integrity mismatch"):
        validate_bundle(root, policy_path=POLICY)

    root, _ = _bundle(tmp_path / "second")
    (root / "unlisted.txt").write_text("unlisted", encoding="utf-8")
    with pytest.raises(ReleaseEvidenceError, match="unlisted files"):
        validate_bundle(root, policy_path=POLICY)


def test_path_traversal_and_secret_bearing_manifest_are_rejected(tmp_path: Path) -> None:
    root, manifest = _bundle(tmp_path)
    test_gate = next(gate for gate in manifest["gates"] if gate["category"] == "test")
    test_gate["artifacts"][0]["path"] = "../result.json"
    _write_manifest(root, manifest)
    with pytest.raises(ReleaseEvidenceError, match="bundle-relative"):
        validate_bundle(root, policy_path=POLICY)

    root, manifest = _bundle(tmp_path / "second")
    manifest["attestation"]["token"] = "live-credential"
    _write_manifest(root, manifest)
    with pytest.raises(ReleaseEvidenceError, match="secret"):
        validate_bundle(root, policy_path=POLICY)


def test_production_profiles_require_immutable_revision_and_deployment_gates(
    tmp_path: Path,
) -> None:
    root, manifest = _bundle(tmp_path)
    manifest["release"]["profile"] = "production-combined"
    _write_manifest(root, manifest)
    with pytest.raises(ReleaseEvidenceError, match="source revision"):
        validate_bundle(root, policy_path=POLICY)

    manifest["release"]["source_revision"] = "sha256:source-revision"
    _write_manifest(root, manifest)
    with pytest.raises(ReleaseEvidenceError, match="required state disagrees"):
        validate_bundle(root, policy_path=POLICY)


def test_published_json_schema_and_policy_cover_the_same_categories() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    policy = json.loads(POLICY.read_text(encoding="utf-8"))

    assert schema["properties"]["schema_version"]["const"] == 1
    assert set(schema["$defs"]["gate"]["properties"]["category"]["enum"]) == CATEGORIES
    assert set(policy["profiles"]["production-combined"]["required_categories"]) == CATEGORIES
