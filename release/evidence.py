"""Validate immutable, machine-readable PhoneAgent release evidence bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import cast

SCHEMA_VERSION = 1
MANIFEST_NAME = "release-evidence.json"
CHECKSUM_NAME = "release-evidence.sha256"
CATEGORIES = frozenset(
    {"test", "evaluation", "benchmark", "security", "migration", "apk", "image", "rollback"}
)
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,119}$")
_SECRET_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r'"(?:api[_-]?key|password|secret|token|authorization)"\s*:\s*"(?!redacted)[^\"]+"',
    re.IGNORECASE,
)


class ReleaseEvidenceError(ValueError):
    """Raised when evidence is incomplete, mutable, unsafe, or does not qualify."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReleaseEvidenceError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ReleaseEvidenceError(f"{label} must be a list")
    return cast(list[object], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseEvidenceError(f"{label} must be a non-empty string")
    return value.strip()


def _exact_keys(
    value: dict[str, object],
    *,
    label: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    missing = required - set(value)
    unknown = set(value) - allowed
    if missing or unknown:
        raise ReleaseEvidenceError(
            f"{label} fields drifted: missing={sorted(missing)} unknown={sorted(unknown)}"
        )


def _timestamp(value: object, label: str) -> datetime:
    text = _string(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseEvidenceError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ReleaseEvidenceError(f"{label} must contain a timezone")
    return parsed


def _load_json(path: Path, label: str) -> dict[str, object]:
    try:
        payload = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseEvidenceError(f"{label} is not valid readable JSON") from exc
    return _object(payload, label)


def _relative_artifact_path(value: object, category: str) -> PurePosixPath:
    text = _string(value, "artifact.path")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or "\\" in text:
        raise ReleaseEvidenceError(f"artifact path is not bundle-relative: {text}")
    if len(path.parts) < 3 or path.parts[:2] != ("evidence", category):
        raise ReleaseEvidenceError(f"artifact path must be below evidence/{category}/: {text}")
    return path


def _load_policy(path: Path, profile: str) -> set[str]:
    policy = _load_json(path, "release evidence policy")
    if policy.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseEvidenceError("unsupported release evidence policy schema")
    profiles = _object(policy.get("profiles"), "policy.profiles")
    definition = _object(profiles.get(profile), f"unknown release profile: {profile}")
    raw_categories = _list(definition.get("required_categories"), "required_categories")
    categories = {_string(item, "required category") for item in raw_categories}
    if not categories <= CATEGORIES:
        raise ReleaseEvidenceError("policy contains an unknown evidence category")
    return categories


def seal_bundle(bundle_dir: Path) -> str:
    """Write the manifest checksum after evidence producers finish the bundle."""

    manifest_path = bundle_dir.resolve() / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ReleaseEvidenceError(f"missing regular {MANIFEST_NAME}")
    digest = sha256(manifest_path)
    (manifest_path.parent / CHECKSUM_NAME).write_text(digest + "\n", encoding="ascii")
    return digest


def validate_bundle(bundle_dir: Path, *, policy_path: Path) -> dict[str, object]:
    """Validate schema, qualification policy, artifact integrity, and bundle closure."""

    root = bundle_dir.resolve()
    manifest_path = root / MANIFEST_NAME
    checksum_path = root / CHECKSUM_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ReleaseEvidenceError(f"missing regular {MANIFEST_NAME}")
    if not checksum_path.is_file() or checksum_path.is_symlink():
        raise ReleaseEvidenceError(f"missing regular {CHECKSUM_NAME}")
    manifest_text = manifest_path.read_text(encoding="utf-8")
    if _SECRET_RE.search(manifest_text):
        raise ReleaseEvidenceError("release evidence manifest appears to contain a secret")
    manifest = _load_json(manifest_path, "release evidence manifest")
    _exact_keys(
        manifest,
        label="manifest",
        required={"schema_version", "release", "environment", "subjects", "gates", "attestation"},
    )
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseEvidenceError("unsupported release evidence schema")

    release = _object(manifest.get("release"), "release")
    _exact_keys(
        release,
        label="release",
        required={
            "release_id",
            "product",
            "version",
            "profile",
            "source_revision",
            "source_tree_sha256",
            "created_at",
        },
    )
    release_id = _string(release.get("release_id"), "release.release_id")
    if not _ID_RE.fullmatch(release_id):
        raise ReleaseEvidenceError("release.release_id has an invalid format")
    _string(release.get("product"), "release.product")
    _string(release.get("version"), "release.version")
    profile = _string(release.get("profile"), "release.profile")
    source_revision = _string(release.get("source_revision"), "release.source_revision")
    source_tree_sha256 = _string(release.get("source_tree_sha256"), "release.source_tree_sha256")
    if not _SHA256_RE.fullmatch(source_tree_sha256):
        raise ReleaseEvidenceError("release.source_tree_sha256 must be SHA-256")
    created_at = _timestamp(release.get("created_at"), "release.created_at")
    required_categories = _load_policy(policy_path, profile)
    if profile != "development" and source_revision == "uncommitted":
        raise ReleaseEvidenceError("candidate and production evidence require a source revision")

    environment = _object(manifest.get("environment"), "environment")
    _exact_keys(
        environment,
        label="environment",
        required={"profile_id", "os", "architecture", "python"},
        optional={"gpu", "device_profile"},
    )
    for field in ("profile_id", "os", "architecture", "python"):
        _string(environment.get(field), f"environment.{field}")

    subjects = _list(manifest.get("subjects"), "subjects")
    subject_kinds: set[str] = set()
    subject_ids: set[tuple[str, str]] = set()
    for raw_subject in subjects:
        subject = _object(raw_subject, "subject")
        _exact_keys(
            subject,
            label="subject",
            required={"kind", "identifier", "digest"},
        )
        kind = _string(subject.get("kind"), "subject.kind")
        if kind not in {"source", "container_image", "android_apk", "model"}:
            raise ReleaseEvidenceError(f"unknown subject kind: {kind}")
        digest = _string(subject.get("digest"), f"subject {kind}.digest")
        if not _DIGEST_RE.fullmatch(digest):
            raise ReleaseEvidenceError(f"subject {kind} digest must be sha256:<hex>")
        identifier = _string(subject.get("identifier"), f"subject {kind}.identifier")
        subject_id = (kind, identifier)
        if subject_id in subject_ids:
            raise ReleaseEvidenceError(f"duplicate release subject: {kind}/{identifier}")
        subject_ids.add(subject_id)
        subject_kinds.add(kind)
    if "source" not in subject_kinds:
        raise ReleaseEvidenceError("release evidence requires a source subject")

    gates = _list(manifest.get("gates"), "gates")
    if len(gates) != len(CATEGORIES):
        raise ReleaseEvidenceError("release evidence requires exactly one gate per category")
    categories_seen: set[str] = set()
    gate_ids: set[str] = set()
    listed_files: set[str] = set()
    passed = 0
    not_applicable = 0
    for raw_gate in gates:
        gate = _object(raw_gate, "gate")
        _exact_keys(
            gate,
            label="gate",
            required={
                "gate_id",
                "category",
                "required",
                "status",
                "started_at",
                "finished_at",
                "executor",
                "summary",
                "metrics",
                "artifacts",
            },
            optional={"not_applicable_reason"},
        )
        gate_id = _string(gate.get("gate_id"), "gate.gate_id")
        if not _ID_RE.fullmatch(gate_id) or gate_id in gate_ids:
            raise ReleaseEvidenceError(f"invalid or duplicate gate id: {gate_id}")
        gate_ids.add(gate_id)
        category = _string(gate.get("category"), f"{gate_id}.category")
        if category not in CATEGORIES or category in categories_seen:
            raise ReleaseEvidenceError(f"unknown or duplicate gate category: {category}")
        categories_seen.add(category)
        required = gate.get("required")
        expected_required = category in required_categories
        if not isinstance(required, bool) or required is not expected_required:
            raise ReleaseEvidenceError(
                f"{category} required state disagrees with profile {profile}"
            )
        status = _string(gate.get("status"), f"{gate_id}.status")
        if status not in {"pass", "fail", "not_applicable"}:
            raise ReleaseEvidenceError(f"unknown gate status: {status}")
        if status == "fail":
            raise ReleaseEvidenceError(f"release contains a failed gate: {gate_id}")
        reason = gate.get("not_applicable_reason")
        if status == "not_applicable":
            if required or not isinstance(reason, str) or len(reason.strip()) < 12:
                raise ReleaseEvidenceError(
                    f"{gate_id} cannot be not_applicable without an optional owned rationale"
                )
            not_applicable += 1
        elif reason is not None:
            raise ReleaseEvidenceError(f"{gate_id} has an unexpected not_applicable_reason")
        else:
            passed += 1
        started = _timestamp(gate.get("started_at"), f"{gate_id}.started_at")
        finished = _timestamp(gate.get("finished_at"), f"{gate_id}.finished_at")
        if finished < started or finished > created_at:
            raise ReleaseEvidenceError(f"{gate_id} timestamps are inconsistent")
        _string(gate.get("executor"), f"{gate_id}.executor")
        _string(gate.get("summary"), f"{gate_id}.summary")
        metrics = _object(gate.get("metrics"), f"{gate_id}.metrics")
        if any(not isinstance(value, str | int | float | bool) for value in metrics.values()):
            raise ReleaseEvidenceError(f"{gate_id}.metrics must contain scalar values")

        artifacts = _list(gate.get("artifacts"), f"{gate_id}.artifacts")
        if status == "pass" and not artifacts:
            raise ReleaseEvidenceError(f"passed gate has no evidence artifact: {gate_id}")
        for raw_artifact in artifacts:
            artifact = _object(raw_artifact, "artifact")
            _exact_keys(
                artifact,
                label="artifact",
                required={
                    "path",
                    "sha256",
                    "bytes",
                    "media_type",
                    "contains_customer_data",
                },
            )
            relative = _relative_artifact_path(artifact.get("path"), category)
            relative_text = relative.as_posix()
            if relative_text in listed_files:
                raise ReleaseEvidenceError(f"artifact is listed more than once: {relative_text}")
            listed_files.add(relative_text)
            expected_hash = _string(artifact.get("sha256"), f"{relative_text}.sha256")
            if not _SHA256_RE.fullmatch(expected_hash):
                raise ReleaseEvidenceError(f"artifact hash is invalid: {relative_text}")
            expected_bytes = artifact.get("bytes")
            if not isinstance(expected_bytes, int) or expected_bytes < 1:
                raise ReleaseEvidenceError(f"artifact size is invalid: {relative_text}")
            _string(artifact.get("media_type"), f"{relative_text}.media_type")
            if artifact.get("contains_customer_data") is not False:
                raise ReleaseEvidenceError(
                    f"artifact customer-data declaration must be false: {relative_text}"
                )
            path = root.joinpath(*relative.parts)
            if not path.is_file() or path.is_symlink():
                raise ReleaseEvidenceError(f"artifact is missing or not regular: {relative_text}")
            if path.stat().st_size != expected_bytes or sha256(path) != expected_hash:
                raise ReleaseEvidenceError(f"artifact integrity mismatch: {relative_text}")

    if categories_seen != set(CATEGORIES):
        raise ReleaseEvidenceError("release evidence categories are incomplete")
    if required_categories and passed == 0:
        raise ReleaseEvidenceError("release has no passing required gates")
    if any(
        category == "apk"
        and _string(_object(raw, "gate").get("status"), "gate.status") == "pass"
        and "android_apk" not in subject_kinds
        for category, raw in (
            (_string(_object(item, "gate").get("category"), "gate.category"), item)
            for item in gates
        )
    ):
        raise ReleaseEvidenceError("a passing APK gate requires an android_apk subject")
    if any(
        category == "image"
        and _string(_object(raw, "gate").get("status"), "gate.status") == "pass"
        and "container_image" not in subject_kinds
        for category, raw in (
            (_string(_object(item, "gate").get("category"), "gate.category"), item)
            for item in gates
        )
    ):
        raise ReleaseEvidenceError("a passing image gate requires a container_image subject")

    attestation = _object(manifest.get("attestation"), "attestation")
    _exact_keys(
        attestation,
        label="attestation",
        required={
            "generated_by",
            "generator_version",
            "generated_at",
            "command",
            "contains_secrets",
            "contains_customer_data",
        },
    )
    _string(attestation.get("generated_by"), "attestation.generated_by")
    if attestation.get("generator_version") != "release-evidence-v1":
        raise ReleaseEvidenceError("unsupported release evidence generator version")
    generated_at = _timestamp(attestation.get("generated_at"), "attestation.generated_at")
    if generated_at != created_at:
        raise ReleaseEvidenceError("attestation and release timestamps must match")
    _string(attestation.get("command"), "attestation.command")
    if attestation.get("contains_secrets") is not False:
        raise ReleaseEvidenceError("release attestation must declare contains_secrets=false")
    if attestation.get("contains_customer_data") is not False:
        raise ReleaseEvidenceError("release attestation must declare contains_customer_data=false")

    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    expected_files = listed_files | {MANIFEST_NAME, CHECKSUM_NAME}
    if actual_files != expected_files:
        raise ReleaseEvidenceError(
            "bundle contains missing or unlisted files: "
            f"expected={sorted(expected_files)} actual={sorted(actual_files)}"
        )
    expected_manifest_hash = checksum_path.read_text(encoding="ascii").strip()
    if not _SHA256_RE.fullmatch(expected_manifest_hash):
        raise ReleaseEvidenceError(f"{CHECKSUM_NAME} must contain one bare SHA-256")
    manifest_hash = sha256(manifest_path)
    if manifest_hash != expected_manifest_hash:
        raise ReleaseEvidenceError("release evidence manifest checksum mismatch")

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "release_id": release_id,
        "profile": profile,
        "manifest_sha256": manifest_hash,
        "gate_count": len(gates),
        "passed_gate_count": passed,
        "not_applicable_gate_count": not_applicable,
        "artifact_count": len(listed_files),
        "subject_count": len(subjects),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument(
        "--policy", type=Path, default=Path(__file__).with_name("evidence-policy.json")
    )
    parser.add_argument(
        "--seal",
        action="store_true",
        help="write the manifest checksum before validating the closed bundle",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.seal:
        seal_bundle(args.bundle)
    summary = validate_bundle(args.bundle, policy_path=args.policy)
    rendered = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
