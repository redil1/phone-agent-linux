#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${HOME}/.config/phone-agent/business-suite.env"
docker compose --project-name phoneagent-suite --env-file "${ENV_FILE}" \
  -f "${ROOT}/integrations/business_suite/compose.yaml" stop
echo "PhoneAgent Business Suite stopped. Persistent data was preserved."

