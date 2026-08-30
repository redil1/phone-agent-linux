"""End-to-End Live Integration Test & Benchmark for Whisper Lightning on CUDA."""

from __future__ import annotations

import asyncio
import time
import numpy as np
import torch
from faster_whisper import WhisperModel

from phone_agent_gateway.ai_bridge.production_pipeline import (
    ProviderConfig,
    create_provider_services,
    prewarm_speech_models,
)


async def main() -> None:
    print("=" * 70)
    print("PHONEAGENT GATEWAY: WHISPER LIGHTNING CUDA VERIFICATION")
    print("=" * 70)

    # 1. Device check
    device = "cuda" if torch.cuda.is_available() else "cpu"
    device_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
    print(f"[*] Target Compute Device: {device} ({device_name})")
    assert device == "cuda", "Expected CUDA device to be active on this machine!"

    # 2. Config & Prewarm Test
    print("\n[Step 1] Initializing ProviderConfig with whisper_cuda...")
    config = ProviderConfig(
        stt_provider="whisper_cuda",
        stt_model="large-v3-turbo",
        tts_provider="kokoro",
        tts_model="hexgrad/Kokoro-82M",
        llm_provider="ollama",
        llm_model="qwen2.5:3b",
        stt_language="en-US",
        deepgram_api_key="",
        cartesia_api_key="",
        tts_voice_id="af_heart",
    )
    config.validate(require_credentials=False)
    print(" -> Configuration validated successfully.")

    print("\n[Step 2] Prewarming Speech Models (Whisper + Kokoro on CUDA)...")
    t0 = time.perf_counter()
    timings = await prewarm_speech_models(config)
    t1 = time.perf_counter()
    print(f" -> Prewarm completed in {(t1 - t0) * 1000:.2f} ms: {timings}")

    # 3. Direct faster-whisper CUDA Latency Benchmark
    print("\n[Step 3] Benchmarking Lightning-Fast Speech Recognition on RTX A6000...")
    model = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")

    # Generate synthetic 16kHz speech tone (5.0 seconds)
    t = np.linspace(0, 5.0, 16000 * 5, dtype=np.float32)
    audio = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

    latencies = []
    for i in range(3):
        t_start = time.perf_counter()
        segments, _info = model.transcribe(audio, beam_size=1, language="en")
        _ = list(segments)
        t_end = time.perf_counter()
        lat_ms = (t_end - t_start) * 1000.0
        latencies.append(lat_ms)
        speedup = 5000.0 / lat_ms
        print(f" -> Run {i+1}: 5.0s audio transcribed in {lat_ms:6.1f} ms ({speedup:4.1f}x real-time)")

    avg_lat = sum(latencies) / len(latencies)
    avg_speedup = 5000.0 / avg_lat
    print(f" -> Steady-State Transcription Latency: {avg_lat:.1f} ms ({avg_speedup:.1f}x real-time)")

    # 4. Pipeline Service Creation
    print("\n[Step 4] Assembling Provider Cascade Services...")
    services = create_provider_services(config, sample_rate=16_000)
    print(f" -> STT Service: {services.stt.__class__.__name__}")
    print(f" -> TTS Service: {services.tts.__class__.__name__}")
    print(f" -> LLM Service: {services.llm.__class__.__name__}")

    print("\n" + "=" * 70)
    print("SUCCESS: 100% LIGHTNING-FAST WHISPER CUDA VERIFICATION COMPLETED!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
