#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_COMPOSE="${PROJECT_DIR}/integrations/openwa/compose.yaml"
SIDECAR_DIR="${HOME}/.local/share/phone-agent/openwa"
CONFIG_DIR="${HOME}/.config/phone-agent"
COMPOSE_TARGET="${SIDECAR_DIR}/compose.yaml"
ENV_FILE="${CONFIG_DIR}/openwa-sidecar.env"

command -v docker >/dev/null || { echo "Docker is required for the OpenWA sidecar." >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker Desktop is not running." >&2; exit 1; }
[[ -f "${SOURCE_COMPOSE}" ]] || { echo "OpenWA compose asset is missing." >&2; exit 1; }

mkdir -p "${SIDECAR_DIR}" "${CONFIG_DIR}"
chmod 700 "${SIDECAR_DIR}" "${CONFIG_DIR}"
cp "${SOURCE_COMPOSE}" "${COMPOSE_TARGET}"
chmod 600 "${COMPOSE_TARGET}"

if [[ ! -f "${ENV_FILE}" ]]; then
  OPENWA_MASTER_KEY="$(openssl rand -base64 48 | tr -d '\n')"
  OPENWA_API_KEY_PEPPER="$(openssl rand -hex 32)"
  /usr/bin/python3 - "${ENV_FILE}" "${OPENWA_MASTER_KEY}" "${OPENWA_API_KEY_PEPPER}" <<'PY'
import os
import sys
from pathlib import Path

target = Path(sys.argv[1])
payload = (
    f"OPENWA_MASTER_KEY={sys.argv[2]}\n"
    f"OPENWA_API_KEY_PEPPER={sys.argv[3]}\n"
    "OPENWA_PORT=2785\n"
)
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    os.write(fd, payload.encode())
    os.fsync(fd)
finally:
    os.close(fd)
PY
fi
chmod 600 "${ENV_FILE}"

docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_TARGET}" pull
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_TARGET}" up -d

HEALTHY=0
for _ in {1..90}; do
  if curl --silent --fail --max-time 2 http://127.0.0.1:2785/api/health/ready >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 1
done
if [[ "${HEALTHY}" -ne 1 ]]; then
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_TARGET}" logs --tail 80 >&2
  echo "OpenWA did not become healthy." >&2
  exit 1
fi

echo "OpenWA sidecar is healthy at http://127.0.0.1:2785/"
echo "Admin key file: ${ENV_FILE}"
echo "Next: open the dashboard, create/pair a session, then provision PhoneAgent in Tools & MCP."
