#!/usr/bin/env bash
set -euo pipefail

STATE_FILE="${HOME}/.config/phone-agent/install-state.json"
APP_TARGET="${HOME}/Applications/PhoneAgent.app"
PLIST_TARGET="${HOME}/Library/LaunchAgents/com.phoneagent.studio.plist"
RUNTIME_TARGET="${HOME}/.local/share/phone-agent/runtime"
LABEL="com.phoneagent.studio"

[[ -f "${STATE_FILE}" && ! -L "${STATE_FILE}" ]] || {
  echo "No recoverable PhoneAgent installation state was found." >&2
  exit 1
}

BACKUP_DIR="$(/usr/bin/python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["backup_dir"])' "${STATE_FILE}")"
[[ "${BACKUP_DIR}" == "${HOME}/.local/share/phone-agent/install-backups/"* ]] || {
  echo "Refusing an unexpected rollback path." >&2
  exit 1
}
[[ -d "${BACKUP_DIR}" && ! -L "${BACKUP_DIR}" ]] || {
  echo "The recorded backup directory is unavailable." >&2
  exit 1
}

RECOVERY_DIR="${HOME}/.local/share/phone-agent/rollback-current/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${RECOVERY_DIR}"
launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true

if [[ -e "${APP_TARGET}" ]]; then
  mv "${APP_TARGET}" "${RECOVERY_DIR}/PhoneAgent.app"
fi
if [[ -e "${PLIST_TARGET}" ]]; then
  mv "${PLIST_TARGET}" "${RECOVERY_DIR}/com.phoneagent.studio.plist"
fi
if [[ -e "${RUNTIME_TARGET}" ]]; then
  mv "${RUNTIME_TARGET}" "${RECOVERY_DIR}/runtime"
fi
if [[ -e "${BACKUP_DIR}/PhoneAgent.app" ]]; then
  mv "${BACKUP_DIR}/PhoneAgent.app" "${APP_TARGET}"
fi
if [[ -e "${BACKUP_DIR}/com.phoneagent.studio.plist" ]]; then
  cp -p "${BACKUP_DIR}/com.phoneagent.studio.plist" "${PLIST_TARGET}"
  launchctl bootstrap "gui/$(id -u)" "${PLIST_TARGET}"
fi
if [[ -e "${BACKUP_DIR}/runtime" ]]; then
  mv "${BACKUP_DIR}/runtime" "${RUNTIME_TARGET}"
fi

echo "Rollback complete. The replaced installation is recoverable at ${RECOVERY_DIR}."
