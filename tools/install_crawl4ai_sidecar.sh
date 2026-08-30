#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_COMPOSE="${PROJECT_DIR}/integrations/crawl4ai/compose.yaml"
SOURCE_CONFIG="${PROJECT_DIR}/integrations/crawl4ai/config.yml"
PHONE_AGENT_USER_HOME="${HOME}"
SIDECAR_DIR="${PHONE_AGENT_USER_HOME}/.local/share/phone-agent/crawl4ai"
CONFIG_DIR="${PHONE_AGENT_USER_HOME}/.config/phone-agent"
COMPOSE_TARGET="${SIDECAR_DIR}/compose.yaml"
CONFIG_TARGET="${SIDECAR_DIR}/config.yml"
ENV_FILE="${CONFIG_DIR}/crawl4ai-sidecar.env"
TOKEN_FILE="${CONFIG_DIR}/crawl4ai-api-token"
WEB_RESEARCH_CONFIG="${CONFIG_DIR}/web-research.json"

command -v docker >/dev/null || { echo "Docker is required for Crawl4AI." >&2; exit 1; }
command -v openssl >/dev/null || { echo "OpenSSL is required to create a token." >&2; exit 1; }
command -v jq >/dev/null || { echo "jq is required to validate Crawl4AI." >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker Desktop is not running." >&2; exit 1; }
[[ -f "${SOURCE_COMPOSE}" ]] || { echo "Crawl4AI compose asset is missing." >&2; exit 1; }
[[ -f "${SOURCE_CONFIG}" ]] || { echo "Crawl4AI config asset is missing." >&2; exit 1; }

mkdir -p "${SIDECAR_DIR}" "${CONFIG_DIR}"
chmod 700 "${SIDECAR_DIR}" "${CONFIG_DIR}"
cp "${SOURCE_COMPOSE}" "${COMPOSE_TARGET}"
cp "${SOURCE_CONFIG}" "${CONFIG_TARGET}"
chmod 600 "${COMPOSE_TARGET}" "${CONFIG_TARGET}"

if [[ ! -f "${TOKEN_FILE}" ]]; then
  umask 077
  openssl rand -base64 48 | tr -d '\n' > "${TOKEN_FILE}"
fi
chmod 600 "${TOKEN_FILE}"

if [[ ! -f "${ENV_FILE}" ]]; then
  /usr/bin/python3 - "${ENV_FILE}" "${TOKEN_FILE}" "${CONFIG_TARGET}" <<'PY'
import os
import sys
from pathlib import Path

target = Path(sys.argv[1])
token_file = Path(sys.argv[2]).resolve()
config_file = Path(sys.argv[3]).resolve()
payload = (
    f"CRAWL4AI_TOKEN_FILE={token_file}\n"
    f"CRAWL4AI_CONFIG_FILE={config_file}\n"
    "CRAWL4AI_PORT=11235\n"
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

if ! grep -q '^CRAWL4AI_CONFIG_FILE=' "${ENV_FILE}"; then
  printf 'CRAWL4AI_CONFIG_FILE=%s\n' "${CONFIG_TARGET}" >> "${ENV_FILE}"
fi

/usr/bin/python3 - "${WEB_RESEARCH_CONFIG}" "${TOKEN_FILE}" <<'PY'
import json
import os
import sys
from pathlib import Path

target = Path(sys.argv[1])
token = Path(sys.argv[2]).read_text(encoding="utf-8").strip()
payload = {}
if target.exists():
    payload = json.loads(target.read_text(encoding="utf-8"))
payload.update(
    {
        "version": 1,
        "crawl4ai_enabled": True,
        "crawl4ai_url": "http://127.0.0.1:11235",
        "crawl4ai_token": token,
    }
)
defaults = {
    "revision": 0,
    "enabled": True,
    "task_ids": [],
    "search_results": 10,
    "pages_to_read": 3,
    "static_concurrency": 3,
    "safe_search": "moderate",
    "language": "auto",
    "country": "US",
    "overall_timeout_ms": 9000,
    "search_timeout_ms": 1800,
    "page_timeout_ms": 3500,
    "max_chars_per_source": 5000,
    "max_total_chars": 14000,
    "cache_ttl_seconds": 600,
    "max_cache_entries": 128,
    "respect_robots_txt": True,
    "preferred_domains": [],
    "blocked_domains": [],
    "duckduckgo_fallback_enabled": True,
    "crawl4ai_timeout_ms": 5000,
    "crawl4ai_max_pages": 2,
}
for key, value in defaults.items():
    payload.setdefault(key, value)
target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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

docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_TARGET}" pull
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_TARGET}" up -d

HEALTHY=0
for _ in {1..120}; do
  if curl --silent --fail --max-time 2 http://127.0.0.1:11235/health >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 1
done
if [[ "${HEALTHY}" -ne 1 ]]; then
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_TARGET}" logs --tail 100 >&2
  echo "Crawl4AI did not become healthy." >&2
  exit 1
fi

TOKEN="$(tr -d '\n' < "${TOKEN_FILE}")"
if ! curl --silent --show-error --fail-with-body --max-time 15 \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  --data '{"urls":["https://example.com"],"browser_config":{"type":"BrowserConfig","params":{"headless":true}},"crawler_config":{"type":"CrawlerRunConfig","params":{"cache_mode":"bypass","page_timeout":5000}}}' \
  http://127.0.0.1:11235/crawl \
  | jq -e '.results | type == "array" and length == 1 and .[0].success == true' \
  >/dev/null; then
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_TARGET}" logs --tail 100 >&2
  echo "Crawl4AI health passed, but the authenticated browser crawl failed." >&2
  exit 1
fi

echo "Crawl4AI is healthy and completed an authenticated browser crawl."
echo "PhoneAgent Web Research was configured privately for http://127.0.0.1:11235."
