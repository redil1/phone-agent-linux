#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${PROJECT_DIR}/integrations/business_suite/compose.yaml"
CONFIG_DIR="${HOME}/.config/phone-agent"
DATA_DIR="${HOME}/.local/share/phone-agent/business-suite"
SECRETS_DIR="${CONFIG_DIR}/business-suite-secrets"
ENV_FILE="${CONFIG_DIR}/business-suite.env"
CRAWL_CONFIG="${DATA_DIR}/crawl4ai-config.yml"
FRAPPE_CONFIG="${CONFIG_DIR}/frappe.json"
WEB_RESEARCH_CONFIG="${CONFIG_DIR}/web-research.json"
PROJECT_NAME="phoneagent-suite"

[[ "$(uname -s)" == "Darwin" ]] || { echo "This installer targets macOS." >&2; exit 1; }
command -v docker >/dev/null || { echo "Install Docker Desktop first." >&2; exit 1; }
command -v openssl >/dev/null || { echo "OpenSSL is required." >&2; exit 1; }
command -v uv >/dev/null || { echo "Install uv first." >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Start Docker Desktop first." >&2; exit 1; }
[[ -f "${COMPOSE_FILE}" ]] || { echo "Business-suite Compose file is missing." >&2; exit 1; }

cd "${PROJECT_DIR}"
uv run ruff check ai_bridge integrations/business_suite/phoneagent_frappe tests tools
uv run python tools/verify_frozen_whatsapp.py
bash -n tools/install_full_business_suite_macos.sh tools/business_suite_status.sh \
  tools/backup_business_suite.sh tools/restore_business_suite.sh \
  tools/stop_full_business_suite.sh integrations/business_suite/scripts/init-site.sh

mkdir -p "${CONFIG_DIR}" "${DATA_DIR}" "${SECRETS_DIR}"
chmod 700 "${CONFIG_DIR}" "${DATA_DIR}" "${SECRETS_DIR}"
cp "${PROJECT_DIR}/integrations/crawl4ai/config.yml" "${CRAWL_CONFIG}"
chmod 600 "${CRAWL_CONFIG}"
docker volume inspect phoneagent-openwa-data >/dev/null 2>&1 || \
  docker volume create phoneagent-openwa-data >/dev/null

secret() {
  local name="$1"
  local mode="$2"
  local path="${SECRETS_DIR}/${name}"
  if [[ ! -s "${path}" ]]; then
    umask 077
    case "${mode}" in
      hex) openssl rand -hex 32 > "${path}" ;;
      key) openssl rand -hex 8 | cut -c1-15 > "${path}" ;;
      *) openssl rand -base64 48 | tr -d '\n' > "${path}" ;;
    esac
  fi
  chmod 600 "${path}"
  printf '%s' "${path}"
}

DB_PASSWORD_FILE="$(secret frappe-db-root password)"
ADMIN_PASSWORD_FILE="$(secret frappe-admin password)"
API_KEY_FILE="$(secret frappe-api-key key)"
API_SECRET_FILE="$(secret frappe-api-secret password)"
CRAWL_TOKEN_FILE="$(secret crawl4ai-api-token password)"

OPENWA_MASTER_KEY=""
OPENWA_API_KEY_PEPPER=""
LEGACY_OPENWA_ENV="${CONFIG_DIR}/openwa-sidecar.env"
if [[ -s "${LEGACY_OPENWA_ENV}" ]]; then
  OPENWA_MASTER_KEY="$(sed -n 's/^OPENWA_MASTER_KEY=//p' "${LEGACY_OPENWA_ENV}" | head -n 1)"
  OPENWA_API_KEY_PEPPER="$(sed -n 's/^OPENWA_API_KEY_PEPPER=//p' "${LEGACY_OPENWA_ENV}" | head -n 1)"
fi
[[ -n "${OPENWA_MASTER_KEY}" ]] || OPENWA_MASTER_KEY="$(openssl rand -base64 48 | tr -d '\n')"
[[ -n "${OPENWA_API_KEY_PEPPER}" ]] || OPENWA_API_KEY_PEPPER="$(openssl rand -hex 32)"
/usr/bin/python3 - \
  "${ENV_FILE}" "${DB_PASSWORD_FILE}" "${ADMIN_PASSWORD_FILE}" \
  "${API_KEY_FILE}" "${API_SECRET_FILE}" "${CRAWL_TOKEN_FILE}" \
  "${CRAWL_CONFIG}" "${OPENWA_MASTER_KEY}" "${OPENWA_API_KEY_PEPPER}" <<'PY'
import os
import sys
from pathlib import Path

target = Path(sys.argv[1])
values = {
    "FRAPPE_SITE_NAME": "phoneagent.localhost",
    "FRAPPE_PORT": "8080",
    "FRAPPE_DB_ROOT_PASSWORD_FILE": sys.argv[2],
    "FRAPPE_ADMIN_PASSWORD_FILE": sys.argv[3],
    "FRAPPE_API_KEY_FILE": sys.argv[4],
    "FRAPPE_API_SECRET_FILE": sys.argv[5],
    "CRAWL4AI_TOKEN_FILE": sys.argv[6],
    "CRAWL4AI_CONFIG_FILE": sys.argv[7],
    "CRAWL4AI_PORT": "11235",
    "OPENWA_MASTER_KEY": sys.argv[8],
    "OPENWA_API_KEY_PEPPER": sys.argv[9],
    "OPENWA_PORT": "2785",
    "BACKUP_RETENTION_DAYS": "30",
}
payload = "".join(f"{key}={value}\n" for key, value in values.items())
temporary = target.with_suffix(".tmp")
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
try:
    os.write(fd, payload.encode())
    os.fsync(fd)
finally:
    os.close(fd)
os.replace(temporary, target)
os.chmod(target, 0o600)
PY

docker compose --project-name "${PROJECT_NAME}" --env-file "${ENV_FILE}" \
  -f "${COMPOSE_FILE}" config --quiet

# Build first so the currently working PhoneAgent and sidecars remain untouched
# if an upstream application or dependency cannot be assembled.
docker compose --project-name "${PROJECT_NAME}" --env-file "${ENV_FILE}" \
  -f "${COMPOSE_FILE}" build backend

# The unified stack reuses the existing OpenWA named volume. Stop only the old
# standalone containers immediately before taking ownership of their ports.
for old in phoneagent-openwa phoneagent-crawl4ai; do
  if docker container inspect "${old}" >/dev/null 2>&1; then
    docker stop "${old}" >/dev/null
  fi
done

docker compose --project-name "${PROJECT_NAME}" --env-file "${ENV_FILE}" \
  -f "${COMPOSE_FILE}" up -d --remove-orphans

healthy=0
for _ in {1..180}; do
  if curl --silent --fail --max-time 3 http://127.0.0.1:8080/api/method/ping >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done
if [[ "${healthy}" -ne 1 ]]; then
  docker compose --project-name "${PROJECT_NAME}" --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" ps >&2
  docker compose --project-name "${PROJECT_NAME}" --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" logs --tail 120 create-site backend frontend >&2
  echo "Frappe business suite did not become healthy." >&2
  exit 1
fi

API_KEY="$(tr -d '\r\n' < "${API_KEY_FILE}")"
API_SECRET="$(tr -d '\r\n' < "${API_SECRET_FILE}")"
/usr/bin/python3 - "${FRAPPE_CONFIG}" "${API_KEY}" "${API_SECRET}" <<'PY'
import json
import os
import sys
from pathlib import Path

target = Path(sys.argv[1])
tool_names = (
    "business_get_customer_context", "business_upsert_current_lead",
    "business_record_call_outcome", "business_create_opportunity",
    "business_schedule_follow_up", "business_search_catalog",
    "business_create_quotation_draft", "business_create_sales_order_draft",
    "business_get_order_status", "business_get_invoice_status",
    "business_create_support_ticket", "business_get_support_status",
    "business_update_support_ticket", "business_mark_do_not_call",
)
payload = {
    "version": 1,
    "revision": 1,
    "enabled": True,
    "base_url": "http://127.0.0.1:8080",
    "site_name": "phoneagent.localhost",
    "api_key": sys.argv[2],
    "api_secret": sys.argv[3],
    "request_timeout_ms": 8000,
    "max_result_items": 10,
    "campaign_autopilot_enabled": True,
    "campaign_poll_seconds": 15,
    "campaign_claim_seconds": 300,
    "tools": [{"name": name, "enabled": True, "task_ids": []} for name in tool_names],
}
temporary = target.with_suffix(".tmp")
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
try:
    os.write(fd, (json.dumps(payload, indent=2) + "\n").encode())
    os.fsync(fd)
finally:
    os.close(fd)
os.replace(temporary, target)
os.chmod(target, 0o600)
PY

CRAWL_TOKEN="$(tr -d '\r\n' < "${CRAWL_TOKEN_FILE}")"
/usr/bin/python3 - "${WEB_RESEARCH_CONFIG}" "${CRAWL_TOKEN}" <<'PY'
import json
import os
import sys
from pathlib import Path

target = Path(sys.argv[1])
payload = json.loads(target.read_text()) if target.exists() else {}
payload.update({
    "version": 1,
    "enabled": True,
    "crawl4ai_enabled": True,
    "crawl4ai_url": "http://127.0.0.1:11235",
    "crawl4ai_token": sys.argv[2],
})
defaults = {
    "revision": 1, "task_ids": [], "search_results": 10, "pages_to_read": 3,
    "static_concurrency": 3, "safe_search": "moderate", "language": "auto",
    "country": "US", "overall_timeout_ms": 9000, "search_timeout_ms": 1800,
    "page_timeout_ms": 3500, "max_chars_per_source": 5000,
    "max_total_chars": 14000, "cache_ttl_seconds": 600, "max_cache_entries": 128,
    "respect_robots_txt": True, "preferred_domains": [], "blocked_domains": [],
    "duckduckgo_fallback_enabled": True, "crawl4ai_timeout_ms": 5000,
    "crawl4ai_max_pages": 2,
}
for key, value in defaults.items():
    payload.setdefault(key, value)
temporary = target.with_suffix(".tmp")
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
try:
    os.write(fd, (json.dumps(payload, indent=2) + "\n").encode())
    os.fsync(fd)
finally:
    os.close(fd)
os.replace(temporary, target)
os.chmod(target, 0o600)
PY

"${PROJECT_DIR}/tools/install_macos.sh"

curl --silent --show-error --fail --max-time 10 \
  -H "Authorization: token ${API_KEY}:${API_SECRET}" \
  -H "X-Frappe-Site-Name: phoneagent.localhost" \
  -H "Content-Type: application/json" \
  --data '{}' \
  http://127.0.0.1:8080/api/method/phoneagent_frappe.api.health \
  | /usr/bin/python3 -c 'import json,sys; assert json.load(sys.stdin)["message"]["status"] == "ok"'

echo "PhoneAgent Business Suite is installed and healthy."
echo "PhoneAgent Studio: http://127.0.0.1:8090/"
echo "CRM: http://127.0.0.1:8080/crm"
echo "Helpdesk: http://127.0.0.1:8080/helpdesk"
echo "ERPNext: http://127.0.0.1:8080/app"
