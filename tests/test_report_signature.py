from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from phone_agent_gateway.release.report_signature import (
    ReportSignatureError,
    sign_report,
    verify_report,
)


def _keys(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    private = root / "private.pem"
    public = root / "public.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private)], check=True
    )
    private.chmod(0o600)
    subprocess.run(
        ["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
        check=True,
    )
    return private, public


def test_ed25519_report_signature_round_trip_and_tamper_rejection(tmp_path: Path) -> None:
    private, public = _keys(tmp_path)
    report = tmp_path / "report.json"
    report.write_text('{"status":"pass"}\n', encoding="utf-8")

    envelope = sign_report(report, private, public)

    assert verify_report(report, envelope, public)["status"] == "pass"
    report.write_text('{"status":"fail"}\n', encoding="utf-8")
    with pytest.raises(ReportSignatureError, match="digest mismatch"):
        verify_report(report, envelope, public)


def test_wrong_public_key_and_malformed_envelope_are_rejected(tmp_path: Path) -> None:
    private, public = _keys(tmp_path)
    _, wrong_public = _keys(tmp_path / "wrong")
    report = tmp_path / "report.json"
    report.write_text("{}\n", encoding="utf-8")
    envelope = sign_report(report, private, public)

    with pytest.raises(ReportSignatureError, match="fingerprint mismatch"):
        verify_report(report, envelope, wrong_public)

    envelope["unexpected"] = True
    with pytest.raises(ReportSignatureError, match="fields drifted"):
        verify_report(report, envelope, public)


def test_private_key_permissions_must_be_restricted(tmp_path: Path) -> None:
    private, public = _keys(tmp_path)
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"status": "pass"}) + "\n", encoding="utf-8")
    private.chmod(0o644)

    with pytest.raises(ReportSignatureError, match="permissions"):
        sign_report(report, private, public)
