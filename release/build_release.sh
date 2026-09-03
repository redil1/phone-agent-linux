#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FINAL_DIR="${PROJECT_DIR}/dist/release"
ALLOW_UNSIGNED=0
[[ "${1:-}" == "--unsigned" ]] && ALLOW_UNSIGNED=1

cd "${PROJECT_DIR}"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  [[ -z "$(git status --porcelain)" ]] || {
    echo "Release requires a clean, committed source tree." >&2
    exit 1
  }
elif [[ "${ALLOW_UNSIGNED}" -eq 0 ]]; then
  echo "Signed release requires an initialized source repository." >&2
  exit 1
else
  echo "Building an unsigned local artifact without source revision metadata." >&2
fi
if [[ "${ALLOW_UNSIGNED}" -eq 0 && -z "${PHONE_AGENT_CODESIGN_IDENTITY:-}" ]]; then
  echo "Set PHONE_AGENT_CODESIGN_IDENTITY or use --unsigned for a local test artifact." >&2
  exit 1
fi

VERSION="$(/usr/bin/python3 -c \
  'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"
STAGING="$(mktemp -d "${TMPDIR:-/tmp}/phoneagent-release.XXXXXX")"
trap 'rm -rf "${STAGING}"' EXIT
ARTIFACTS="${STAGING}/phone-agent-${VERSION}"
mkdir -p "${ARTIFACTS}/python" "${ARTIFACTS}/macos" "${ARTIFACTS}/qualification"

uv sync --locked --all-extras --dev
uv run python tools/verify_frozen_whatsapp.py
uv run ruff check ai_bridge integrations/business_suite/phoneagent_frappe mac_client qualification release tests tools
STAGING="${STAGING}" python -c 'import os; from pathlib import Path; p=Path("ai_bridge/web_static/index.html").read_text(); Path(os.environ["STAGING"], "studio.js").write_text(p.rsplit("<script>",1)[1].split("</script>",1)[0])'
node --check "${STAGING}/studio.js"
uv run pytest -q
uv build --out-dir "${ARTIFACTS}/python"
uv export --preview-features sbom-export --locked --all-extras --format cyclonedx1.5 \
  --output-file "${ARTIFACTS}/cyclonedx-sbom.json"
uv export --locked --all-extras --no-emit-project --format requirements.txt \
  --output-file "${ARTIFACTS}/requirements.lock.txt"
uv tool run --from pip-audit==2.10.1 pip-audit \
  --requirement "${ARTIFACTS}/requirements.lock.txt" --no-deps --disable-pip \
  --strict --progress-spinner off --format json \
  --output "${ARTIFACTS}/dependency-audit.json"

PHONE_AGENT_BUILD_OUTPUT="${STAGING}/desktop" ./desktop_app/build_app.sh >/dev/null
ditto -c -k --sequesterRsrc --keepParent \
  "${STAGING}/desktop/PhoneAgent.app" "${ARTIFACTS}/macos/PhoneAgent.app.zip"
cp qualification/devices/*.json "${ARTIFACTS}/qualification/"
cp docs/SECURITY_AND_OPERATIONS.md "${ARTIFACTS}/"
cp docs/IDENTITY_KERNEL.md "${ARTIFACTS}/"
cp docs/WEBUI_USER_GUIDE.md "${ARTIFACTS}/"
cp docs/CALL_CONTEXT_STRATEGY.md "${ARTIFACTS}/"
cp docs/BUSINESS_SUITE.md "${ARTIFACTS}/"
cp docs/EXTERNAL_AGENT_CONTROL_PLANE.md "${ARTIFACTS}/"
cp docs/HERMES_PHONEAGENT_SETUP.md "${ARTIFACTS}/"
mkdir -p "${ARTIFACTS}/business-suite"
ditto "${PROJECT_DIR}/integrations/business_suite" "${ARTIFACTS}/business-suite/integrations"
cp tools/install_full_business_suite_macos.sh tools/business_suite_status.sh \
  tools/backup_business_suite.sh tools/restore_business_suite.sh \
  tools/stop_full_business_suite.sh "${ARTIFACTS}/business-suite/"
mkdir -p "${ARTIFACTS}/skills"
ditto "${PROJECT_DIR}/skills/phoneagent-master" "${ARTIFACTS}/skills/phoneagent-master"

if [[ "${ALLOW_UNSIGNED}" -eq 0 ]]; then
  codesign --verify --deep --strict "${STAGING}/desktop/PhoneAgent.app"
  if [[ -n "${PHONE_AGENT_NOTARY_PROFILE:-}" ]]; then
    xcrun notarytool submit "${ARTIFACTS}/macos/PhoneAgent.app.zip" \
      --keychain-profile "${PHONE_AGENT_NOTARY_PROFILE}" --wait
    xcrun stapler staple "${STAGING}/desktop/PhoneAgent.app"
    ditto -c -k --sequesterRsrc --keepParent \
      "${STAGING}/desktop/PhoneAgent.app" "${ARTIFACTS}/macos/PhoneAgent.app.zip"
  fi
fi

uv run python release/generate_manifest.py "${ARTIFACTS}" --version "${VERSION}"
(
  cd "${ARTIFACTS}"
  shasum -a 256 -c SHA256SUMS
)

mkdir -p "$(dirname "${FINAL_DIR}")"
if [[ -e "${FINAL_DIR}" ]]; then
  mv "${FINAL_DIR}" "${FINAL_DIR}.backup.$(date -u +%Y%m%dT%H%M%SZ)"
fi
mv "${ARTIFACTS}" "${FINAL_DIR}"
echo "Release ready: ${FINAL_DIR}"
