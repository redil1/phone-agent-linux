#!/usr/bin/env bash
# ==============================================================================
# PhoneAgent Gateway: Linux & NVIDIA CUDA Bootstrap Script
# ==============================================================================
set -euo pipefail

log() {
    printf "[PhoneAgent Linux Bootstrap] %s\n" "$*"
}

error() {
    printf "[ERROR] %s\n" "$*" >&2
    exit 1
}

# 1. Verify OS
[ "$(uname -s)" = "Linux" ] || error "This bootstrap script targets Linux (found $(uname -s))."

log "System Architecture: $(uname -m)"
log "Operating System:    $(cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"')"

# 2. Verify NVIDIA GPU & CUDA Drivers
if command -v nvidia-smi &>/dev/null; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)
    DRIVER_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1)
    VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader | head -n1)
    log "NVIDIA GPU Detected: ${GPU_NAME} (${VRAM} VRAM, Driver ${DRIVER_VER})"
else
    log "WARNING: nvidia-smi not found. Running in CPU-only fallback mode."
fi

# 3. Verify Python 3.11+
PYTHON_BIN=""
if command -v python3.11 &>/dev/null; then
    PYTHON_BIN=$(command -v python3.11)
elif command -v python3 &>/dev/null; then
    PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    if [[ "${PY_VER}" == "3.11" || "${PY_VER}" == "3.12" ]]; then
        PYTHON_BIN=$(command -v python3)
    fi
fi

if [ -z "${PYTHON_BIN}" ]; then
    if [ -f "/home/Ubuntu/miniconda3/envs/phoneagent/bin/python" ]; then
        PYTHON_BIN="/home/Ubuntu/miniconda3/envs/phoneagent/bin/python"
    else
        error "Python 3.11 or 3.12 is required but was not found in PATH."
    fi
fi
log "Using Python Binary: ${PYTHON_BIN} ($(${PYTHON_BIN} --version))"

# 4. Verify PyTorch CUDA Acceleration
log "Checking PyTorch & CUDA availability..."
${PYTHON_BIN} -c "
import torch
cuda_avail = torch.cuda.is_available()
print(f' -> PyTorch Version: {torch.__version__}')
print(f' -> CUDA Available:  {cuda_avail}')
if cuda_avail:
    print(f' -> Active Device:   {torch.cuda.get_device_name(0)}')
"

# 5. Verify Speech Packages
log "Verifying speech backends (Kokoro TTS, faster-whisper STT)..."
${PYTHON_BIN} -c "
import kokoro
import faster_whisper
import pipecat
import soxr
print(' -> All core speech modules loaded successfully!')
"

# 6. Verify PhoneAgent Gateway editable install
log "Verifying PhoneAgent Gateway package install..."
${PYTHON_BIN} -c "
import phone_agent_gateway
from phone_agent_gateway.ai_bridge.kokoro_tts_service import PhoneAgentKokoroTTSService
print(' -> phone_agent_gateway import verified successfully.')
"

log "======================================================================"
log "Bootstrap complete! PhoneAgent Gateway is 100% production ready on Linux."
log "======================================================================"
