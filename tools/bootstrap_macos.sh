#!/usr/bin/env bash
# One command to take a fresh clone on a new Mac to a running PhoneAgent Studio.
#
# This installs everything that can be installed from software alone. The parts
# that need hardware or an account you own -- the rooted Android handset, the
# shared link key, Antigravity, the business suite -- are reported at the end
# with the exact command for each, rather than half-attempted here.
#
# Safe to re-run: every step is idempotent and the macOS installer keeps its own
# rollback snapshot.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_DIR}"

SKIP_TESTS=0
SKIP_SERVICE=0
for arg in "$@"; do
    case "$arg" in
        --skip-tests) SKIP_TESTS=1 ;;
        --skip-service) SKIP_SERVICE=1 ;;
        -h|--help)
            echo "Usage: $0 [--skip-tests] [--skip-service]"
            echo "  --skip-tests    do not run the suite before installing"
            echo "  --skip-service  set up the environment but do not install the LaunchAgent"
            exit 0
            ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

step() { printf '\n\033[1m[%s]\033[0m %s\n' "$1" "$2"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------- preflight

step "1/5" "Checking this machine"

[ "$(uname -s)" = "Darwin" ] || die "PhoneAgent runs on macOS only."

ARCH="$(uname -m)"
if [ "${ARCH}" != "arm64" ]; then
    # MLX has no Intel build at all, so Kokoro and Parakeet cannot load. Failing
    # here is kinder than failing later inside a call.
    die "Apple Silicon is required (found ${ARCH}). Kokoro and Parakeet run on MLX/Metal, which has no Intel build."
fi
ok "Apple Silicon ($(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo arm64))"

MACOS_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
[ "${MACOS_MAJOR}" -ge 14 ] || warn "macOS ${MACOS_MAJOR} is older than the reviewed baseline (14+)."
ok "macOS $(sw_vers -productVersion)"

command -v xcrun >/dev/null 2>&1 && xcrun --find swiftc >/dev/null 2>&1 \
    || die "Xcode Command Line Tools are missing. Run: xcode-select --install"
ok "Xcode Command Line Tools"

if ! command -v uv >/dev/null 2>&1; then
    warn "uv is not installed; installing it now."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="${HOME}/.local/bin:${PATH}"
    command -v uv >/dev/null 2>&1 || die "uv installation did not complete. See https://docs.astral.sh/uv/"
fi
ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"

# Optional, reported rather than enforced: each one gates a feature, not the app.
OPTIONAL_MISSING=()
command -v adb    >/dev/null 2>&1 || OPTIONAL_MISSING+=("adb (Android platform-tools) - required for GSM calls")
command -v cargo  >/dev/null 2>&1 || OPTIONAL_MISSING+=("cargo (Rust) - required for the direct WhatsApp voice sidecar")
command -v docker >/dev/null 2>&1 || OPTIONAL_MISSING+=("docker - required for the Frappe CRM/ERP + OpenWA business suite")
[ -d "/Applications/Antigravity.app" ] || OPTIONAL_MISSING+=("Antigravity.app - required for the zero-key Gemini LLM and live STT")

# ------------------------------------------------------------- python env

step "2/5" "Installing the locked Python environment"
uv sync --locked --all-extras --dev
ok "$(uv run python -c 'import importlib.metadata as m; print(len(list(m.distributions())))' 2>/dev/null || echo '?') packages from uv.lock"

step "3/5" "Verifying the frozen WhatsApp boundary"
uv run python tools/verify_frozen_whatsapp.py
ok "frozen pipeline intact"

# ------------------------------------------------------------------ verify

if [ "${SKIP_TESTS}" -eq 0 ]; then
    step "4/5" "Running lint and the full test suite"
    uv run ruff check ai_bridge mac_client qualification release tests tools
    ok "ruff clean"
    uv run pytest -q
    ok "test suite passed"
else
    step "4/5" "Skipping tests (--skip-tests)"
fi

# ----------------------------------------------------------------- install

if [ "${SKIP_SERVICE}" -eq 0 ]; then
    step "5/5" "Installing the macOS app and Studio service"
    ./tools/install_macos.sh
else
    step "5/5" "Skipping the service install (--skip-service)"
    warn "Run ./tools/install_macos.sh when you are ready."
fi

# ------------------------------------------------------------- what is left

printf '\n\033[1m========================================================\033[0m\n'
printf '\033[1m PhoneAgent software install complete\033[0m\n'
printf '\033[1m========================================================\033[0m\n'
printf '\n  Studio: http://127.0.0.1:8090\n'

if [ "${#OPTIONAL_MISSING[@]}" -gt 0 ]; then
    printf '\n\033[33mOptional components not found on this machine:\033[0m\n'
    for item in "${OPTIONAL_MISSING[@]}"; do
        printf '  - %s\n' "$item"
    done
fi

cat <<'NEXT'

Remaining steps need hardware or an account, so they are not automated:

  1. Provider access (pick one)
       Antigravity   open Antigravity.app and sign in - no API key needed
       Any other     put keys in ~/.config/phone-agent/secrets.env, mode 0600
                     e.g.  GEMINI_API_KEY=...        (Google TTS voice only)

  2. Rooted Android handset, for GSM calls
       ./android_service_apk/build_and_install.sh          # build the APK
       ./android_service_apk/install_privileged.sh --commit # live overlay, lost on reboot
       ./android_service_apk/provision_link_key.sh          # shared PHAG link key

     For an install that survives reboots, bake the APK into the system image:
       ./android_service_apk/build_persistent_gsi.sh --base-image <pristine>.img \
           --output artifacts/persistent-gsi/system-phoneagent.img
       ./android_service_apk/flash_persistent_gsi.sh --serial <SERIAL> \
           --image <built>.img --rollback-image <pristine>.img \
           --link-key ~/.config/phone-agent/link.key --commit

  3. Business suite, for CRM/ERP, WhatsApp and web research tools
       ./tools/install_full_business_suite_macos.sh

  4. Direct WhatsApp voice channel (optional, separate from GSM)
       cd whatsapp_channel/rust_caller && ./build.sh

  5. Confirm the device is ready
       uv run phone-agent-qualify --ensure-forwards

Models (~2.9 GB: Parakeet, Kokoro, Supertonic) download on first use.
NEXT
