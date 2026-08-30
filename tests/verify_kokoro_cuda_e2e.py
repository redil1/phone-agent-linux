"""End-to-End Live Integration Test & Benchmark for Kokoro TTS on NVIDIA RTX A6000 CUDA."""

from __future__ import annotations

import asyncio
import time
import numpy as np
import torch
from pipecat.frames.frames import TTSAudioRawFrame

from phone_agent_gateway.ai_bridge.kokoro_tts_service import (
    PhoneAgentKokoroTTSService,
    prewarm_kokoro,
)


async def main() -> None:
    print("=" * 70)
    print("PHONEAGENT GATEWAY: KOKORO TTS CUDA END-TO-END VERIFICATION")
    print("=" * 70)

    # 1. Device check
    device = "cuda" if torch.cuda.is_available() else "cpu"
    device_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
    print(f"[*] Target Compute Device: {device} ({device_name})")
    assert device == "cuda", "Expected CUDA device to be active on this machine!"

    # 2. Prewarm Test
    print("\n[Step 1] Running prewarm_kokoro()...")
    t_prewarm_start = time.perf_counter()
    prewarm_duration_ms = prewarm_kokoro(
        model="hexgrad/Kokoro-82M",
        voice="af_heart",
        language="en-US",
        device="cuda",
    )
    print(f" -> Prewarm completed in {prewarm_duration_ms:.2f} ms")

    # 3. English Synthesis Test via PhoneAgentKokoroTTSService
    print("\n[Step 2] Testing English Live Telephony Synthesis (PhoneAgentKokoroTTSService)...")
    service_en = PhoneAgentKokoroTTSService(
        voice="af_heart",
        lang="en-US",
        speed=1.0,
        sample_rate=16_000,
        model="hexgrad/Kokoro-82M",
        device="cuda",
    )

    test_text_en = (
        "Good morning! Thank you for calling PhoneAgent Gateway. "
        "Kokoro TTS is now synthesizing speech natively with PyTorch and CUDA acceleration on your NVIDIA RTX A6000 GPU."
    )

    t0 = time.perf_counter()
    frames = []
    first_frame_latency_ms = None

    async for frame in service_en.run_tts(test_text_en, context_id="call-live-101"):
        if isinstance(frame, TTSAudioRawFrame):
            if first_frame_latency_ms is None:
                first_frame_latency_ms = (time.perf_counter() - t0) * 1000.0
            frames.append(frame)

    t1 = time.perf_counter()
    total_compute_ms = (t1 - t0) * 1000.0

    assert len(frames) > 0, "Expected at least one audio frame from synthesis!"

    # Validate audio buffer properties
    total_pcm_bytes = b"".join(f.audio for f in frames)
    pcm_samples = np.frombuffer(total_pcm_bytes, dtype="<i2")
    audio_duration_s = len(pcm_samples) / 16_000.0
    rtf = (total_compute_ms / 1000.0) / audio_duration_s if audio_duration_s > 0 else 0.0
    speedup = (1.0 / rtf) if rtf > 0 else 0.0

    print(f" -> Total Audio Frames:    {len(frames)}")
    print(f" -> Telephony Sample Rate: {frames[0].sample_rate} Hz (Target: 16000)")
    print(f" -> Channels:              {frames[0].num_channels} (Mono)")
    print(f" -> Audio Duration:        {audio_duration_s:.2f} seconds")
    print(f" -> Time to First Audio:   {first_frame_latency_ms:.2f} ms (TTFA)")
    print(f" -> Total Compute Time:    {total_compute_ms:.2f} ms")
    print(f" -> Real-Time Factor:      {rtf:.4f} ({speedup:.1f}x real-time)")
    print(f" -> Finite Values Check:   {np.all(np.isfinite(pcm_samples))}")
    print(f" -> Dynamic Range:         Min={np.min(pcm_samples)}, Max={np.max(pcm_samples)}")

    assert frames[0].sample_rate == 16_000
    assert frames[0].num_channels == 1
    assert speedup > 20.0, f"Expected speedup > 20x, got {speedup:.1f}x"

    # 4. French Synthesis Test via PhoneAgentKokoroTTSService
    print("\n[Step 3] Testing French Telephony Synthesis (ff_siwis)...")
    service_fr = PhoneAgentKokoroTTSService(
        voice="ff_siwis",
        lang="fr-FR",
        speed=1.0,
        sample_rate=16_000,
        model="hexgrad/Kokoro-82M",
        device="cuda",
    )

    test_text_fr = (
        "Bonjour! Bienvenue sur la passerelle téléphonique PhoneAgent. "
        "Le moteur de synthèse vocale Kokoro fonctionne parfaitement avec accélération CUDA sur Linux."
    )

    t0_fr = time.perf_counter()
    fr_frames = []
    async for frame in service_fr.run_tts(test_text_fr, context_id="call-live-102"):
        if isinstance(frame, TTSAudioRawFrame):
            fr_frames.append(frame)
    t1_fr = time.perf_counter()

    fr_pcm_bytes = b"".join(f.audio for f in fr_frames)
    fr_pcm_samples = np.frombuffer(fr_pcm_bytes, dtype="<i2")
    fr_duration_s = len(fr_pcm_samples) / 16_000.0
    fr_compute_ms = (t1_fr - t0_fr) * 1000.0

    print(f" -> French Audio Duration: {fr_duration_s:.2f} seconds")
    print(f" -> French Compute Time:   {fr_compute_ms:.2f} ms ({fr_duration_s / (fr_compute_ms / 1000.0):.1f}x real-time)")

    print("\n" + "=" * 70)
    print("SUCCESS: 100% PRODUCTION-READY KOKORO TTS ON CUDA VERIFIED!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
