#!/usr/bin/env python3
"""Fail closed if the qualified WhatsApp implementation changes.

The production hardening project deliberately surrounds the working WhatsApp
channel. It must not silently edit signaling, media, codec, Android routing, or
the bundled sidecar. Updating this manifest is a separate, explicit
requalification event—not part of ordinary feature development.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "release" / "frozen-whatsapp.sha256"


def verify() -> list[str]:
    failures: list[str] = []
    for number, raw in enumerate(MANIFEST.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            expected, relative = line.split(None, 1)
        except ValueError:
            failures.append(f"manifest line {number} is malformed")
            continue
        relative = relative.strip()
        path = ROOT / relative
        if not path.is_file():
            failures.append(f"missing frozen file: {relative}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            failures.append(f"frozen WhatsApp file changed: {relative}")
    return failures


def main() -> int:
    failures = verify()
    if failures:
        print("\n".join(failures))
        print(
            "WhatsApp is frozen. Restore the qualified bytes or run an explicit "
            "WhatsApp requalification project."
        )
        return 1
    print("frozen-whatsapp-ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
