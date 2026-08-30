#!/usr/bin/env bash
#
# Set up, verify and start PhoneAgent.
#
#   ./run.sh                 sync, lint, test, then start the Studio
#   ./run.sh -s              skip lint and tests, start immediately
#   ./run.sh --check         verify only; do not start the app
#   ./run.sh --port 9000     serve the Studio on another port
#   ./run.sh --no-log        do not write a call log
#
# The call log matters: connection drops and audio-quality counters are only
# recoverable from it after the fact.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PORT=8090
RUN_CHECKS=1
START_APP=1
WRITE_LOG=1
LOG_DIR="${HOME}/phone-agent-logs"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
fail() { printf '\033[31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }
ok()   { printf '\033[32m  ✓ %s\033[0m\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -s|--skip-tests) RUN_CHECKS=0; shift ;;
    --check)         START_APP=0; shift ;;
    --no-log)        WRITE_LOG=0; shift ;;
    --port)          PORT="${2:?--port needs a value}"; shift 2 ;;
    -h|--help)       awk 'NR>2 && /^#/ {sub(/^# ?/, ""); print; next} NR>2 {exit}' "$0"
                     exit 0 ;;
    *)               fail "unknown option: $1  (try --help)" ;;
  esac
done

# ---------------------------------------------------------------- prerequisites
bold "PhoneAgent"
command -v uv >/dev/null 2>&1 || fail \
  "uv is not installed. Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"

# ---------------------------------------------------------------- dependencies
# All three extras. A bare `uv sync` drops pytest (dev) and faster_whisper
# (local), which silently breaks the test suite.
bold "Dependencies"
uv sync --extra dev --extra local --extra cloud --quiet
ok "synced (dev + local + cloud)"

# ---------------------------------------------------------------------- checks
if [[ "${RUN_CHECKS}" -eq 1 ]]; then
  bold "Checks"
  uv run ruff check ai_bridge tests >/dev/null && ok "lint clean"
  # Judge by pytest's exit status, not by grepping its summary line: a collection
  # error or an interrupted run prints no "failed" and would slip through.
  results="$(mktemp)"
  trap 'rm -f "${results}"' EXIT
  if uv run pytest -q >"${results}" 2>&1; then
    ok "$(tail -1 "${results}")"
  else
    tail -25 "${results}" >&2
    fail "tests failed — fix them before placing a call"
  fi
else
  warn "skipping lint and tests (-s)"
fi

# ------------------------------------------------------------------ call setup
bold "Call setup"
if command -v adb >/dev/null 2>&1; then
  if [[ -n "$(adb devices 2>/dev/null | awk 'NR>1 && $2=="device"')" ]]; then
    ok "phone connected over adb"
  else
    warn "no phone detected — the Studio runs, but calls will not dial"
  fi
else
  warn "adb not found — the Studio runs, but calls will not dial"
fi

if [[ -f product_research/main.py ]]; then
  ok "product research engine bundled"
else
  warn "product_research/ is missing — the Product Research tab will be disabled"
fi

if [[ -z "${OPENAI_API_KEY:-}" ]] && [[ ! -f "${HOME}/.codex/auth.json" ]]; then
  warn "no OPENAI_API_KEY and no ~/.codex/auth.json — speech-to-speech cannot authenticate"
fi

[[ "${START_APP}" -eq 1 ]] || { bold "Checks complete; not starting (--check)"; exit 0; }

# The Studio only reports connection drops as they happen. Without a log there
# is nothing left to read afterwards.
LOG_PATH=""
if [[ "${WRITE_LOG}" -eq 1 ]]; then
  mkdir -p "${LOG_DIR}"
  LOG_PATH="${LOG_DIR}/call-$(date +%Y%m%d-%H%M%S).log"
  ok "logging to ${LOG_PATH}"
fi

# ----------------------------------------------------------------------- start
bold "Studio"
info "http://127.0.0.1:${PORT}"
info "Ctrl-C to stop"
echo

export PHONE_AGENT_WEB_PORT="${PORT}"
if [[ -n "${LOG_PATH}" ]]; then
  # Fail on the server's exit status, not tee's.
  set -o pipefail
  uv run phone-agent-web 2>&1 | tee "${LOG_PATH}"
else
  uv run phone-agent-web
fi
