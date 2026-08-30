#!/usr/bin/env bash
set -euo pipefail

SITE_NAME="${FRAPPE_SITE_NAME:?FRAPPE_SITE_NAME is required}"
DB_ROOT_PASSWORD="$(tr -d '\r\n' < /run/secrets/frappe_db_root_password)"
ADMIN_PASSWORD="$(tr -d '\r\n' < /run/secrets/frappe_admin_password)"
API_KEY="$(tr -d '\r\n' < /run/secrets/frappe_api_key)"
API_SECRET="$(tr -d '\r\n' < /run/secrets/frappe_api_secret)"

for required in DB_ROOT_PASSWORD ADMIN_PASSWORD API_KEY API_SECRET; do
  [[ -n "${!required}" ]] || { echo "${required} secret is empty" >&2; exit 1; }
done

wait-for-it -t 180 "${DB_HOST:-db}:${DB_PORT:-3306}"
wait-for-it -t 180 "${REDIS_CACHE:-redis-cache}:6379"
wait-for-it -t 180 "${REDIS_QUEUE:-redis-queue}:6379"

for _ in {1..60}; do
  [[ -s sites/common_site_config.json ]] && break
  sleep 2
done
[[ -s sites/common_site_config.json ]] || {
  echo "Frappe common site configuration was not created" >&2
  exit 1
}

if [[ ! -d "sites/${SITE_NAME}" ]]; then
  bench new-site "${SITE_NAME}" \
    --mariadb-user-host-login-scope=% \
    --db-root-password "${DB_ROOT_PASSWORD}" \
    --admin-password "${ADMIN_PASSWORD}" \
    --no-mariadb-socket
fi

installed="$(bench --site "${SITE_NAME}" list-apps --format text)"
for app in erpnext telephony crm helpdesk phoneagent_frappe; do
  if ! grep -Fxq "${app}" <<<"${installed}"; then
    bench --site "${SITE_NAME}" install-app "${app}"
    installed="$(bench --site "${SITE_NAME}" list-apps --format text)"
  fi
done

bench --site "${SITE_NAME}" migrate
bench --site "${SITE_NAME}" set-config mute_emails 1
bench --site "${SITE_NAME}" set-config allow_cors '["http://127.0.0.1:8090"]' --parse
bench --site "${SITE_NAME}" execute \
  phoneagent_frappe.setup.provision_integration \
  --kwargs "$(jq -cn \
    --arg api_key "${API_KEY}" \
    --arg api_secret "${API_SECRET}" \
    '{api_key:$api_key,api_secret:$api_secret}')" >/dev/null

bench --site "${SITE_NAME}" clear-cache
echo "PhoneAgent Frappe site is initialized: ${SITE_NAME}"

