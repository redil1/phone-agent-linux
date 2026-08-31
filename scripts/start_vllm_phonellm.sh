#!/usr/bin/env bash
set -euo pipefail

MODEL_NAME="pipecat-ai/phonellm-alpha-1"
PORT=8000
HOST="0.0.0.0"
GPU_MEM_UTIL=0.75
MAX_LEN=32768

echo "Starting vLLM server for ${MODEL_NAME} on port ${PORT} with prefix caching..."
exec /home/Ubuntu/miniconda3/envs/phoneagent/bin/python3.11 -m vllm.entrypoints.openai.api_server \
    --model "${MODEL_NAME}" \
    --host "${HOST}" \
    --port "${PORT}" \
    --trust-remote-code \
    --enable-prefix-caching \
    --gpu-memory-utilization "${GPU_MEM_UTIL}" \
    --max-model-len "${MAX_LEN}" \
    --disable-log-requests
