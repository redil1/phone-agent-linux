"""Generate deterministic release artifact metadata and checksums."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    root = args.directory.resolve()
    artifacts = [
        {
            "path": str(path.relative_to(root)),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name not in {"SHA256SUMS", "release-manifest.json"}
    ]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        commit = "uncommitted"
    manifest = {
        "schema_version": 1,
        "product": "phone-agent-gateway",
        "version": args.version,
        "source_commit": commit,
        "artifacts": artifacts,
    }
    manifest_path = root / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    checksum_lines = [f"{item['sha256']}  {item['path']}" for item in artifacts]
    checksum_lines.append(f"{sha256(manifest_path)}  release-manifest.json")
    (root / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n")


if __name__ == "__main__":
    main()
