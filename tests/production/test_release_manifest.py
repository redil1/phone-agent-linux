from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def test_release_manifest_hashes_every_artifact(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("one")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "two.bin").write_bytes(b"two")
    script = Path(__file__).resolve().parents[2] / "release" / "generate_manifest.py"
    subprocess.run([sys.executable, str(script), str(tmp_path), "--version", "9.9.9"], check=True)
    manifest = json.loads((tmp_path / "release-manifest.json").read_text())
    assert manifest["version"] == "9.9.9"
    assert {item["path"] for item in manifest["artifacts"]} == {
        "nested/two.bin",
        "one.txt",
    }
    checksums = (tmp_path / "SHA256SUMS").read_text()
    assert hashlib.sha256(b"one").hexdigest() in checksums
    assert "release-manifest.json" in checksums
