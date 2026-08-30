#!/usr/bin/env bash
set -euo pipefail

PHONE_AGENT_USER_HOME="${HOME}"
SIDECAR_DIR="${PHONE_AGENT_USER_HOME}/.local/share/phone-agent/crawl4ai"
ENV_FILE="${PHONE_AGENT_USER_HOME}/.config/phone-agent/crawl4ai-sidecar.env"
docker compose --env-file "${ENV_FILE}" -f "${SIDECAR_DIR}/compose.yaml" stop
