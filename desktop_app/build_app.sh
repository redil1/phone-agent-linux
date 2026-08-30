#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUTPUT_DIR="${PHONE_AGENT_BUILD_OUTPUT:-${PROJECT_DIR}/build}"
FINAL_APP="${OUTPUT_DIR}/PhoneAgent.app"
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/phoneagent-app.XXXXXX")"
trap 'rm -rf "${TEMP_DIR}"' EXIT

APP="${TEMP_DIR}/PhoneAgent.app"
mkdir -p "${APP}/Contents/MacOS" "${APP}/Contents/Resources" "${OUTPUT_DIR}"
cp "${SCRIPT_DIR}/Info.plist" "${APP}/Contents/Info.plist"
VERSION="$(awk -F'"' '/^version = "/ {print $2; exit}' "${PROJECT_DIR}/pyproject.toml")"
[[ -n "${VERSION}" ]] || { echo "Could not read project version." >&2; exit 1; }
APP_VERSION="${VERSION}" APP_PLIST="${APP}/Contents/Info.plist" /usr/bin/python3 - <<'PY'
import os
import plistlib
from pathlib import Path

plist_path = Path(os.environ["APP_PLIST"])
version = os.environ["APP_VERSION"]
with plist_path.open("rb") as stream:
    payload = plistlib.load(stream)
payload["CFBundleShortVersionString"] = version
major, minor, patch = (int(part) for part in version.split(".")[:3])
payload["CFBundleVersion"] = str(major * 10000 + minor * 100 + patch)
with plist_path.open("wb") as stream:
    plistlib.dump(payload, stream, sort_keys=True)
PY
swiftc "${SCRIPT_DIR}/PhoneAgentDesktop.swift" \
  -o "${APP}/Contents/MacOS/PhoneAgent" \
  -framework Cocoa -framework WebKit -O -whole-module-optimization

if [[ -n "${PHONE_AGENT_CODESIGN_IDENTITY:-}" ]]; then
  codesign --force --options runtime --timestamp \
    --sign "${PHONE_AGENT_CODESIGN_IDENTITY}" "${APP}"
else
  codesign --force --sign - "${APP}"
fi
codesign --verify --deep --strict "${APP}"

if [[ -e "${FINAL_APP}" ]]; then
  BACKUP="${OUTPUT_DIR}/PhoneAgent.app.backup.$(date +%Y%m%d%H%M%S)"
  mv "${FINAL_APP}" "${BACKUP}"
fi
mv "${APP}" "${FINAL_APP}"
printf '%s\n' "${FINAL_APP}"
