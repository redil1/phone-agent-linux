#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${HOME}/.config/phone-agent/business-suite.env"
BACKUP_ROOT="${HOME}/.local/share/phone-agent/business-suite-backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${BACKUP_ROOT}/${STAMP}"
mkdir -p "${TARGET}"
chmod 700 "${BACKUP_ROOT}" "${TARGET}"

docker compose --project-name phoneagent-suite --env-file "${ENV_FILE}" \
  -f "${ROOT}/integrations/business_suite/compose.yaml" --profile tools run --rm backup
docker compose --project-name phoneagent-suite --env-file "${ENV_FILE}" \
  -f "${ROOT}/integrations/business_suite/compose.yaml" \
  cp backend:/home/frappe/frappe-bench/sites/phoneagent.localhost/private/backups/. "${TARGET}/"
cp "${ENV_FILE}" "${TARGET}/business-suite.env"
chmod 600 "${TARGET}"/*
echo "Business-suite backup created: ${TARGET}"

