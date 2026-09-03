"""Create and verify detached Ed25519 signatures for immutable JSON evidence reports."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, cast

SIGNATURE_SCHEMA_VERSION = 1
ALGORITHM = "Ed25519"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class ReportSignatureError(ValueError):
    """Raised when report signing material or verification is invalid."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_openssl(args: list[str], *, label: str) -> bytes:
    try:
        result = subprocess.run(
            ["openssl", *args], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReportSignatureError(f"OpenSSL {label} is unavailable") from exc
    if result.returncode != 0:
        diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReportSignatureError(f"OpenSSL {label} failed: {diagnostic[:300]}")
    return result.stdout


def public_key_fingerprint(public_key: Path) -> str:
    if not public_key.is_file() or public_key.is_symlink():
        raise ReportSignatureError("public key must be a regular file")
    der = _run_openssl(
        ["pkey", "-pubin", "-in", str(public_key), "-outform", "DER"],
        label="public-key decoding",
    )
    return hashlib.sha256(der).hexdigest()


def sign_report(report: Path, private_key: Path, public_key: Path) -> dict[str, Any]:
    if not report.is_file() or report.is_symlink():
        raise ReportSignatureError("report must be a regular file")
    if not private_key.is_file() or private_key.is_symlink():
        raise ReportSignatureError("private key must be a regular file")
    if private_key.stat().st_mode & 0o077:
        raise ReportSignatureError("private key permissions must be 0600 or stricter")
    signature = _run_openssl(
        ["pkeyutl", "-sign", "-rawin", "-inkey", str(private_key), "-in", str(report)],
        label="report signing",
    )
    envelope: dict[str, Any] = {
        "schema_version": SIGNATURE_SCHEMA_VERSION,
        "algorithm": ALGORITHM,
        "report_sha256": sha256(report),
        "public_key_sha256": public_key_fingerprint(public_key),
        "signature_base64": base64.b64encode(signature).decode("ascii"),
        "contains_secrets": False,
        "contains_customer_data": False,
    }
    verify_report(report, envelope, public_key)
    return envelope


def _load_envelope(path: Path) -> dict[str, Any]:
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportSignatureError("signature envelope is unavailable or invalid") from exc
    if not isinstance(raw, dict):
        raise ReportSignatureError("signature envelope must be an object")
    return cast(dict[str, Any], raw)


def verify_report(
    report: Path, signature: Path | dict[str, Any], public_key: Path
) -> dict[str, Any]:
    if not report.is_file() or report.is_symlink():
        raise ReportSignatureError("report must be a regular file")
    envelope = _load_envelope(signature) if isinstance(signature, Path) else signature
    required = {
        "schema_version",
        "algorithm",
        "report_sha256",
        "public_key_sha256",
        "signature_base64",
        "contains_secrets",
        "contains_customer_data",
    }
    if set(envelope) != required:
        raise ReportSignatureError("signature envelope fields drifted")
    if envelope.get("schema_version") != SIGNATURE_SCHEMA_VERSION:
        raise ReportSignatureError("signature schema is unsupported")
    if envelope.get("algorithm") != ALGORITHM:
        raise ReportSignatureError("signature algorithm is unsupported")
    expected_report_hash = envelope.get("report_sha256")
    expected_key_hash = envelope.get("public_key_sha256")
    if not isinstance(expected_report_hash, str) or not _SHA256_RE.fullmatch(expected_report_hash):
        raise ReportSignatureError("signature report hash is invalid")
    if not isinstance(expected_key_hash, str) or not _SHA256_RE.fullmatch(expected_key_hash):
        raise ReportSignatureError("signature public-key hash is invalid")
    if envelope.get("contains_secrets") is not False:
        raise ReportSignatureError("signature envelope must declare contains_secrets=false")
    if envelope.get("contains_customer_data") is not False:
        raise ReportSignatureError("signature envelope must declare contains_customer_data=false")
    if sha256(report) != expected_report_hash:
        raise ReportSignatureError("signed report digest mismatch")
    if public_key_fingerprint(public_key) != expected_key_hash:
        raise ReportSignatureError("signature public-key fingerprint mismatch")
    encoded = envelope.get("signature_base64")
    if not isinstance(encoded, str):
        raise ReportSignatureError("signature bytes are missing")
    try:
        signature_bytes = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ReportSignatureError("signature bytes are not valid base64") from exc
    if len(signature_bytes) != 64:
        raise ReportSignatureError("Ed25519 signature must contain 64 bytes")
    with tempfile.NamedTemporaryFile(prefix="phoneagent-report-", suffix=".sig") as temporary:
        temporary.write(signature_bytes)
        temporary.flush()
        _run_openssl(
            [
                "pkeyutl",
                "-verify",
                "-rawin",
                "-pubin",
                "-inkey",
                str(public_key),
                "-sigfile",
                temporary.name,
                "-in",
                str(report),
            ],
            label="report verification",
        )
    return {
        "schema_version": SIGNATURE_SCHEMA_VERSION,
        "status": "pass",
        "algorithm": ALGORITHM,
        "report_sha256": expected_report_hash,
        "public_key_sha256": expected_key_hash,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    sign = subparsers.add_parser("sign")
    sign.add_argument("report", type=Path)
    sign.add_argument("--private-key", type=Path, required=True)
    sign.add_argument("--public-key", type=Path, required=True)
    sign.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("report", type=Path)
    verify.add_argument("--signature", type=Path, required=True)
    verify.add_argument("--public-key", type=Path, required=True)
    verify.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.operation == "sign":
        result = sign_report(args.report, args.private_key, args.public_key)
        _write_json(args.output, result)
    else:
        result = verify_report(args.report, args.signature, args.public_key)
        if args.output:
            _write_json(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
