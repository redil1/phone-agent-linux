#!/usr/bin/env bash
set -euo pipefail

SIDECAR_DIR="${HOME}/.local/share/phone-agent/openwa"
ENV_FILE="${HOME}/.config/phone-agent/openwa-sidecar.env"
docker compose --env-file "${ENV_FILE}" -f "${SIDECAR_DIR}/compose.yaml" stop
