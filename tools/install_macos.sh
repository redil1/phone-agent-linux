#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_TARGET="${HOME}/Applications/PhoneAgent.app"
PLIST_TARGET="${HOME}/Library/LaunchAgents/com.phoneagent.studio.plist"
BACKUP_ROOT="${HOME}/.local/share/phone-agent/install-backups"
RUNTIME_TARGET="${HOME}/.local/share/phone-agent/runtime"
STATE_FILE="${HOME}/.config/phone-agent/install-state.json"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="${BACKUP_ROOT}/${TIMESTAMP}"
RUNTIME_STAGE="${BACKUP_ROOT}/.runtime-stage-${TIMESTAMP}"
PLIST_STAGE="${BACKUP_ROOT}/.launch-agent-stage-${TIMESTAMP}.plist"
LOG_DIR="${HOME}/phone-agent-logs"
LABEL="com.phoneagent.studio"
WHATSAPP_BINARY_SOURCE="${PROJECT_DIR}/whatsapp_channel/whatsapp-rust-caller"
DEPLOY_STARTED=0
INSTALL_COMPLETE=0
HAD_APP=0
HAD_PLIST=0
HAD_RUNTIME=0

restore_previous_installation() {
  local failed_dir="${BACKUP_DIR}/failed-install"
  launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
  mkdir -p "${failed_dir}"
  if [[ -e "${APP_TARGET}" ]]; then
    mv "${APP_TARGET}" "${failed_dir}/PhoneAgent.app"
  fi
  if [[ -e "${PLIST_TARGET}" ]]; then
    mv "${PLIST_TARGET}" "${failed_dir}/com.phoneagent.studio.plist"
  fi
  if [[ -e "${RUNTIME_TARGET}" ]]; then
    mv "${RUNTIME_TARGET}" "${failed_dir}/runtime"
  fi
  if [[ "${HAD_APP}" -eq 1 && -e "${BACKUP_DIR}/PhoneAgent.app" ]]; then
    mv "${BACKUP_DIR}/PhoneAgent.app" "${APP_TARGET}"
  fi
  if [[ "${HAD_PLIST}" -eq 1 && -e "${BACKUP_DIR}/com.phoneagent.studio.plist" ]]; then
    cp -p "${BACKUP_DIR}/com.phoneagent.studio.plist" "${PLIST_TARGET}"
  fi
  if [[ "${HAD_RUNTIME}" -eq 1 && -e "${BACKUP_DIR}/runtime" ]]; then
    mv "${BACKUP_DIR}/runtime" "${RUNTIME_TARGET}"
  fi
  if [[ -e "${PLIST_TARGET}" ]]; then
    launchctl bootstrap "gui/$(id -u)" "${PLIST_TARGET}" >/dev/null 2>&1 || true
  fi
}

cleanup() {
  local status=$?
  if [[ "${INSTALL_COMPLETE}" -ne 1 && "${DEPLOY_STARTED}" -eq 1 ]]; then
    echo "Installation activation failed; restoring the previous installation." >&2
    restore_previous_installation
  fi
  if [[ -e "${RUNTIME_STAGE}" ]]; then
    mv "${RUNTIME_STAGE}" "${BACKUP_DIR}/unused-runtime-stage"
  fi
  if [[ -e "${PLIST_STAGE}" ]]; then
    mv "${PLIST_STAGE}" "${BACKUP_DIR}/unused-launch-agent-stage.plist"
  fi
  exit "${status}"
}
trap cleanup EXIT

[[ "$(uname -s)" == "Darwin" ]] || { echo "PhoneAgent desktop requires macOS." >&2; exit 1; }
command -v uv >/dev/null || { echo "Install uv before PhoneAgent." >&2; exit 1; }
command -v swiftc >/dev/null || { echo "Install Xcode Command Line Tools." >&2; exit 1; }

# The checked-in qualified executable is Apple-silicon native. Intel Macs build
# the same frozen Rust source locally instead of attempting to launch an
# incompatible binary. The frozen-source verifier still runs before this step.
HOST_ARCH="$(uname -m)"
case "${HOST_ARCH}" in
  arm64) REQUIRED_BINARY_ARCH="arm64" ;;
  x86_64) REQUIRED_BINARY_ARCH="x86_64" ;;
  *) echo "Unsupported Mac architecture: ${HOST_ARCH}" >&2; exit 1 ;;
esac
if [[ ! -x "${WHATSAPP_BINARY_SOURCE}" ]] || \
   ! file "${WHATSAPP_BINARY_SOURCE}" | grep -F "${REQUIRED_BINARY_ARCH}" >/dev/null; then
  command -v cargo >/dev/null || {
    echo "This Intel Mac needs Rustup/Cargo to build the frozen WhatsApp sidecar." >&2
    echo "Install Rust from https://rustup.rs and run this installer again." >&2
    exit 1
  }
  cargo build --locked --release \
    --manifest-path "${PROJECT_DIR}/whatsapp_channel/rust_caller/Cargo.toml"
  WHATSAPP_BINARY_SOURCE="${PROJECT_DIR}/whatsapp_channel/rust_caller/target/release/phoneagent-whatsapp-rust-caller"
  file "${WHATSAPP_BINARY_SOURCE}" | grep -F "${REQUIRED_BINARY_ARCH}" >/dev/null || {
    echo "The locally built WhatsApp sidecar does not match ${HOST_ARCH}." >&2
    exit 1
  }
fi

mkdir -p "${BACKUP_DIR}" "$(dirname "${APP_TARGET}")" \
  "$(dirname "${PLIST_TARGET}")" "$(dirname "${STATE_FILE}")" \
  "$(dirname "${RUNTIME_TARGET}")" "${LOG_DIR}"
chmod 700 "${BACKUP_ROOT}" "$(dirname "${STATE_FILE}")"

cd "${PROJECT_DIR}"
uv sync --locked --all-extras --dev
uv run python tools/verify_frozen_whatsapp.py
uv run ruff check ai_bridge mac_client tests tools
uv run pytest -q
PHONE_AGENT_BUILD_OUTPUT="${PROJECT_DIR}/build" ./desktop_app/build_app.sh

# Build a self-contained runtime outside Desktop. macOS background services do
# not automatically receive Files & Folders access to a user's Desktop, even
# when an interactive shell does.
mkdir -p "${RUNTIME_STAGE}/wheels"
uv build --wheel --out-dir "${RUNTIME_STAGE}/wheels"
RUNTIME_WHEEL="$(find "${RUNTIME_STAGE}/wheels" -maxdepth 1 -name '*.whl' -print -quit)"
[[ -n "${RUNTIME_WHEEL}" ]] || { echo "Runtime wheel was not built." >&2; exit 1; }
uv venv --python 3.12 --relocatable "${RUNTIME_STAGE}/.venv"
uv pip install --python "${RUNTIME_STAGE}/.venv/bin/python" \
  "${RUNTIME_WHEEL}[cloud,local]"
ditto "${PROJECT_DIR}/product_research" "${RUNTIME_STAGE}/product_research"
mkdir -p "${RUNTIME_STAGE}/whatsapp_channel"
cp "${WHATSAPP_BINARY_SOURCE}" \
  "${RUNTIME_STAGE}/whatsapp_channel/whatsapp-rust-caller"
chmod 700 "${RUNTIME_STAGE}/whatsapp_channel/whatsapp-rust-caller"
"${RUNTIME_STAGE}/.venv/bin/python" -c \
  'import importlib.metadata; assert importlib.metadata.version("phone-agent-gateway")'
chmod 700 "${RUNTIME_STAGE}"

PROJECT_DIR="${PROJECT_DIR}" RUNTIME_TARGET="${RUNTIME_TARGET}" \
  PLIST_TARGET="${PLIST_STAGE}" LOG_DIR="${LOG_DIR}" \
  /usr/bin/python3 - <<'PY'
import os
import plistlib
from pathlib import Path

project = Path(os.environ["PROJECT_DIR"])
runtime = Path(os.environ["RUNTIME_TARGET"])
target = Path(os.environ["PLIST_TARGET"])
logs = Path(os.environ["LOG_DIR"])
payload = {
    "Label": "com.phoneagent.studio",
    "ProgramArguments": [str(runtime / ".venv/bin/phone-agent-web")],
    "WorkingDirectory": str(runtime),
    "EnvironmentVariables": {
        "PATH": f"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:{runtime / '.venv/bin'}",
        "PHONE_AGENT_WEB_HOST": "127.0.0.1",
        "PHONE_AGENT_WEB_PORT": "8090",
        "PHONE_AGENT_IDENTITY_PROPOSALS_ENABLED": "true",
        "PHONE_AGENT_PRODUCT_ENGINE_DIR": str(runtime / "product_research"),
        "PHONE_AGENT_WHATSAPP_BINARY": str(
            runtime / "whatsapp_channel/whatsapp-rust-caller"
        ),
    },
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ThrottleInterval": 5,
    "ProcessType": "Interactive",
    "StandardOutPath": str(logs / "studio.out.log"),
    "StandardErrorPath": str(logs / "studio.err.log"),
}
with target.open("wb") as stream:
    plistlib.dump(payload, stream, sort_keys=True)
target.chmod(0o600)
PY

plutil -lint "${PLIST_STAGE}"
codesign --verify --deep --strict "${PROJECT_DIR}/build/PhoneAgent.app"

# Activate only after every staged artifact has passed its offline checks. If
# launchd activation or the health probe fails, the EXIT trap restores the
# exact previous app, runtime and LaunchAgent snapshot.
launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
DEPLOY_STARTED=1
if [[ -e "${APP_TARGET}" ]]; then
  HAD_APP=1
  mv "${APP_TARGET}" "${BACKUP_DIR}/PhoneAgent.app"
fi
if [[ -e "${PLIST_TARGET}" ]]; then
  HAD_PLIST=1
  cp -p "${PLIST_TARGET}" "${BACKUP_DIR}/com.phoneagent.studio.plist"
fi
if [[ -e "${RUNTIME_TARGET}" ]]; then
  HAD_RUNTIME=1
  mv "${RUNTIME_TARGET}" "${BACKUP_DIR}/runtime"
fi

ditto "${PROJECT_DIR}/build/PhoneAgent.app" "${APP_TARGET}"
mv "${RUNTIME_STAGE}" "${RUNTIME_TARGET}"
mv "${PLIST_STAGE}" "${PLIST_TARGET}"
head -n 1 "${RUNTIME_TARGET}/.venv/bin/phone-agent-web" | grep -Fx '#!/bin/sh' >/dev/null
"${RUNTIME_TARGET}/.venv/bin/phone-agent-web" --help >/dev/null
codesign --verify --deep --strict "${APP_TARGET}"
launchctl bootstrap "gui/$(id -u)" "${PLIST_TARGET}"
launchctl kickstart -k "gui/$(id -u)/${LABEL}"

HEALTHY=0
for _ in {1..45}; do
  if curl --silent --show-error --fail --max-time 2 \
    "http://127.0.0.1:8090/api/status" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 1
done
if [[ "${HEALTHY}" -ne 1 ]]; then
  tail -n 40 "${LOG_DIR}/studio.err.log" >&2 2>/dev/null || true
  echo "PhoneAgent Studio did not pass its loopback health check." >&2
  exit 1
fi
launchctl print "gui/$(id -u)/${LABEL}" | \
  grep -F "program = ${RUNTIME_TARGET}/.venv/bin/phone-agent-web" >/dev/null

BACKUP_DIR="${BACKUP_DIR}" PROJECT_DIR="${PROJECT_DIR}" STATE_FILE="${STATE_FILE}" \
  /usr/bin/python3 - <<'PY'
import json
import os
from pathlib import Path

state = {
    "version": 1,
    "project_dir": os.environ["PROJECT_DIR"],
    "backup_dir": os.environ["BACKUP_DIR"],
}
target = Path(os.environ["STATE_FILE"])
temporary = target.with_suffix(".tmp")
temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
temporary.chmod(0o600)
temporary.replace(target)
PY

INSTALL_COMPLETE=1
echo "PhoneAgent installed and running."
echo "Open ${APP_TARGET} or http://127.0.0.1:8090/"
echo "Rollback: ${PROJECT_DIR}/tools/rollback_macos.sh"
