#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${HOME}/.config/phone-agent/business-suite.env"
COMPOSE_FILE="${ROOT}/integrations/business_suite/compose.yaml"
BACKUP_DIR="${1:-}"
CONFIRM="${2:-}"

[[ -d "${BACKUP_DIR}" ]] || {
  echo "Usage: $0 /absolute/backup/directory --confirm" >&2
  exit 2
}
[[ "${CONFIRM}" == "--confirm" ]] || {
  echo "Restore replaces current CRM/ERP data. Re-run with --confirm after reviewing the backup." >&2
  exit 2
}

database="$(find "${BACKUP_DIR}" -maxdepth 1 -type f -name '*-database.sql.gz' -print -quit)"
public_files="$(find "${BACKUP_DIR}" -maxdepth 1 -type f -name '*-files.tgz' ! -name '*-private-files.tgz' -print -quit)"
private_files="$(find "${BACKUP_DIR}" -maxdepth 1 -type f -name '*-private-files.tgz' -print -quit)"
[[ -f "${database}" && -f "${public_files}" && -f "${private_files}" ]] || {
  echo "Backup directory is incomplete." >&2
  exit 1
}

# Always retain a fresh recovery point immediately before replacement.
"${ROOT}/tools/backup_business_suite.sh"

docker compose --project-name phoneagent-suite --env-file "${ENV_FILE}" \
  -f "${COMPOSE_FILE}" stop frontend backend websocket queue-short queue-long scheduler

restore_dir="/home/frappe/frappe-bench/sites/phoneagent.localhost/private/backups/restore"
docker compose --project-name phoneagent-suite --env-file "${ENV_FILE}" \
  -f "${COMPOSE_FILE}" run --rm --no-deps --entrypoint bash create-site -lc \
  "mkdir -p '${restore_dir}'"
for source in "${database}" "${public_files}" "${private_files}"; do
  docker compose --project-name phoneagent-suite --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" cp "${source}" \
    "create-site:${restore_dir}/$(basename "${source}")"
done

db_password="$(tr -d '\r\n' < "${HOME}/.config/phone-agent/business-suite-secrets/frappe-db-root")"
db_name="${restore_dir}/$(basename "${database}")"
public_name="${restore_dir}/$(basename "${public_files}")"
private_name="${restore_dir}/$(basename "${private_files}")"
docker compose --project-name phoneagent-suite --env-file "${ENV_FILE}" \
  -f "${COMPOSE_FILE}" run --rm --no-deps --entrypoint bash create-site -lc \
  "bench --site phoneagent.localhost restore '${db_name}' \
    --db-root-password '${db_password}' \
    --with-public-files '${public_name}' \
    --with-private-files '${private_name}' && \
   bench --site phoneagent.localhost migrate && \
   bench --site phoneagent.localhost clear-cache"

docker compose --project-name phoneagent-suite --env-file "${ENV_FILE}" \
  -f "${COMPOSE_FILE}" up -d

for _ in {1..60}; do
  curl -fsS --max-time 3 http://127.0.0.1:8080/api/method/ping >/dev/null 2>&1 && {
    echo "Business Suite restored and healthy."
    exit 0
  }
  sleep 2
done
echo "Restore completed, but the health check failed. Inspect business_suite_status.sh." >&2
exit 1

