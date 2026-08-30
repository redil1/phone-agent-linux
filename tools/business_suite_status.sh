#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${HOME}/.config/phone-agent/business-suite.env"
docker compose --project-name phoneagent-suite --env-file "${ENV_FILE}" \
  -f "${ROOT}/integrations/business_suite/compose.yaml" ps
curl -fsS http://127.0.0.1:8080/api/method/ping >/dev/null
curl -fsS http://127.0.0.1:8090/api/status

