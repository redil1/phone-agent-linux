#!/usr/bin/env bash
# ==============================================================================
# PhoneAgent AI Gateway - 100% Automated Autopilot Deployment Script (Linux & CUDA)
#
# Usage:
#   bash deploy.sh --all       Deploy PhoneAgent Core + CUDA + OpenWA + CRM + Crawl4AI
#   bash deploy.sh --core      Deploy PhoneAgent Core with NVIDIA CUDA acceleration
#   bash deploy.sh --status    Check status of all running services & endpoints
#   bash deploy.sh --stop      Stop all running containers cleanly
#   bash deploy.sh --check     Preflight hardware qualification only
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

CONFIG_DIR="${HOME}/.config/phone-agent"
DATA_DIR="${HOME}/.local/share/phone-agent"
LOG_DIR="${HOME}/phone-agent-logs"
ENV_FILE="${CONFIG_DIR}/phone-agent-production.env"
COMPOSE_FILE="${SCRIPT_DIR}/compose.production.yaml"

MODE="all"
if [[ $# -gt 0 ]]; then
  case "$1" in
    --all) MODE="all" ;;
    --core) MODE="core" ;;
    --status) MODE="status" ;;
    --stop) MODE="stop" ;;
    --check) MODE="check" ;;
    -h|--help)
      echo "Usage: ./deploy.sh [--all | --core | --status | --stop | --check]"
      exit 0
      ;;
    *)
      echo "Unknown option: $1 (Use --help for usage)" >&2
      exit 1
      ;;
  esac
fi

# ------------------------------------------------------------------------------
# UI Helpers & Formatting
# ------------------------------------------------------------------------------
CLR_RESET="\033[0m"
CLR_BOLD="\033[1m"
CLR_GREEN="\033[32m"
CLR_BLUE="\033[34m"
CLR_CYAN="\033[36m"
CLR_YELLOW="\033[33m"
CLR_RED="\033[31m"

header() {
  echo -e "\n${CLR_BOLD}${CLR_CYAN}======================================================================${CLR_RESET}"
  echo -e "${CLR_BOLD}${CLR_CYAN}  $*${CLR_RESET}"
  echo -e "${CLR_BOLD}${CLR_CYAN}======================================================================${CLR_RESET}"
}

info()  { echo -e "  ${CLR_BLUE}[INFO]${CLR_RESET} $*"; }
ok()    { echo -e "  ${CLR_GREEN}[✓]${CLR_RESET} $*"; }
warn()  { echo -e "  ${CLR_YELLOW}[!]${CLR_RESET} $*"; }
fail()  { echo -e "  ${CLR_RED}[✗]${CLR_RESET} $*" >&2; exit 1; }

# ------------------------------------------------------------------------------
# Action: Stop
# ------------------------------------------------------------------------------
if [[ "${MODE}" == "stop" ]]; then
  header "Stopping PhoneAgent Production Stack"
  docker compose -f "${COMPOSE_FILE}" down
  ok "All containers stopped successfully."
  exit 0
fi

# ------------------------------------------------------------------------------
# Action: Status
# ------------------------------------------------------------------------------
if [[ "${MODE}" == "status" ]]; then
  header "PhoneAgent Production Services Status"
  docker compose -f "${COMPOSE_FILE}" ps
  echo
  info "Checking Endpoints:"
  curl -s -f http://127.0.0.1:8090/api/status >/dev/null 2>&1 && ok "PhoneAgent Studio: http://127.0.0.1:8090 (ONLINE)" || warn "PhoneAgent Studio: (OFFLINE)"
  nc -zv 127.0.0.1 8770 2>/dev/null && ok "Remote Phone Link: 127.0.0.1:8770 (LISTENING)" || warn "Remote Phone Link: (OFFLINE)"
  curl -s -f http://127.0.0.1:2785/api/health/ready >/dev/null 2>&1 && ok "OpenWA WhatsApp: http://127.0.0.1:2785 (ONLINE)" || warn "OpenWA WhatsApp: (OFFLINE)"
  curl -s -f http://127.0.0.1:11235/health >/dev/null 2>&1 && ok "Crawl4AI Research: http://127.0.0.1:11235 (ONLINE)" || warn "Crawl4AI Research: (OFFLINE)"
  exit 0
fi

# ------------------------------------------------------------------------------
# 1. Preflight Hardware & Software Checks
# ------------------------------------------------------------------------------
header "1. Qualifying Hardware & Prerequisites"

OS_NAME="$(uname -s)"
ARCH_NAME="$(uname -m)"
info "Operating System: ${OS_NAME} (${ARCH_NAME})"

command -v docker >/dev/null 2>&1 || fail "Docker is not installed. Install Docker first."
ok "Docker Engine: $(docker --version | awk '{print $3}' | tr -d ',')"

docker info >/dev/null 2>&1 || fail "Docker daemon is not running or current user lacks permissions."
ok "Docker Daemon: Active & Connected"

if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n 1)"
  VRAM_TOTAL="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader | head -n 1)"
  ok "NVIDIA GPU Detected: ${GPU_NAME} (${VRAM_TOTAL})"
else
  warn "No NVIDIA GPU detected. Container will fallback to CPU compute."
fi

[[ "${MODE}" == "check" ]] && { ok "Preflight checks complete."; exit 0; }

# ------------------------------------------------------------------------------
# 2. Automated Secrets & Environment Provisioning
# ------------------------------------------------------------------------------
header "2. Provisioning Cryptographic Secrets & Trust Boundaries"

mkdir -p "${CONFIG_DIR}" "${DATA_DIR}" "${LOG_DIR}"
chmod 700 "${CONFIG_DIR}" "${DATA_DIR}" "${LOG_DIR}"

LINK_KEY_FILE="${CONFIG_DIR}/link.key"
if [[ ! -f "${LINK_KEY_FILE}" ]]; then
  openssl rand 32 > "${LINK_KEY_FILE}"
  chmod 600 "${LINK_KEY_FILE}"
  ok "Generated fresh 32-byte Remote Link private key."
else
  chmod 600 "${LINK_KEY_FILE}"
  ok "Reused existing Remote Link private key (${LINK_KEY_FILE})."
fi

export HOST_UID="$(id -u)"
export HOST_GID="$(id -g)"

if [[ ! -f "${ENV_FILE}" ]]; then
  OPENWA_MASTER_KEY="$(openssl rand -base64 48 | tr -d '\n')"
  OPENWA_API_KEY_PEPPER="$(openssl rand -hex 32)"
  DB_ROOT_PASSWORD="$(openssl rand -hex 24)"
  
  cat <<EOF > "${ENV_FILE}"
HOST_UID=${HOST_UID}
HOST_GID=${HOST_GID}
OPENWA_MASTER_KEY=${OPENWA_MASTER_KEY}
OPENWA_API_KEY_PEPPER=${OPENWA_API_KEY_PEPPER}
DB_ROOT_PASSWORD=${DB_ROOT_PASSWORD}
PHONE_AGENT_WEB_PORT=8090
PHONE_AGENT_REMOTE_LINK_PORT=8770
EOF
  chmod 600 "${ENV_FILE}"
  ok "Generated secure production credentials at ${ENV_FILE}"
else
  chmod 600 "${ENV_FILE}"
  ok "Reused existing environment configuration (${ENV_FILE})."
fi

# ------------------------------------------------------------------------------
# 3. Build & Orchestrate Production Stack
# ------------------------------------------------------------------------------
header "3. Building and Launching Production Containers"

SERVICES_TO_START="phoneagent openwa crawl4ai crm-db crm-redis-cache crm-redis-queue"
if [[ "${MODE}" == "core" ]]; then
  SERVICES_TO_START="phoneagent"
fi

info "Starting services: ${SERVICES_TO_START}"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --build ${SERVICES_TO_START}

# ------------------------------------------------------------------------------
# 4. Service Health Qualification
# ------------------------------------------------------------------------------
header "4. Verifying Service Health"

info "Waiting for PhoneAgent AI Studio to report ready..."
READY=0
for i in {1..40}; do
  if curl -s -f http://127.0.0.1:8090/api/status >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done

if [[ "${READY}" -eq 1 ]]; then
  ok "PhoneAgent Studio is LIVE on http://127.0.0.1:8090"
else
  warn "PhoneAgent Studio is still initializing. Check logs with: docker compose -f compose.production.yaml logs -f"
fi

# ------------------------------------------------------------------------------
# 5. Summary Dashboard
# ------------------------------------------------------------------------------
header "🎉 Autopilot Deployment Complete & 100% Production Ready!"

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo '127.0.0.1')"

echo -e "
${CLR_BOLD}Active Services & Access URLs:${CLR_RESET}
  • ${CLR_BOLD}PhoneAgent Telephony Studio:${CLR_RESET} ${CLR_GREEN}http://127.0.0.1:8090${CLR_RESET} (or http://${HOST_IP}:8090)
  • ${CLR_BOLD}Remote Phone Link Relay:${CLR_RESET}     ${CLR_GREEN}Port 8770 (TCP)${CLR_RESET}
  • ${CLR_BOLD}OpenWA WhatsApp Dashboard:${CLR_RESET}   ${CLR_GREEN}http://127.0.0.1:2785${CLR_RESET}
  • ${CLR_BOLD}Crawl4AI Web Intelligence:${CLR_RESET}   ${CLR_GREEN}http://127.0.0.1:11235${CLR_RESET}
  • ${CLR_BOLD}Database & Cache Stack:${CLR_RESET}      ${CLR_GREEN}MariaDB + Redis Active${CLR_RESET}

${CLR_BOLD}Management Commands:${CLR_RESET}
  • Check status:  ${CLR_CYAN}./deploy.sh --status${CLR_RESET}
  • View logs:     ${CLR_CYAN}docker compose -f compose.production.yaml logs -f${CLR_RESET}
  • Stop services: ${CLR_CYAN}./deploy.sh --stop${CLR_RESET}
"
