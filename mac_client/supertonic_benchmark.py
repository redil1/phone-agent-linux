"""Reproducible local Supertonic 3 versus Supertonic 2 phone-audio benchmark."""

from __future__ import annotations

import argparse
import json
import platform
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from supertonic import TTS

from phone_agent_gateway.ai_bridge.supertonic_tts_service import _waveform_to_pcm16

SAMPLE_RATE = 16_000
CASES = (
    ("en", "Hello. I can help you with that right away."),
    ("fr", "Bonjour. Je peux vous aider avec cela tout de suite."),
    (
        "en",
        "I understand your question. Let me check the details carefully and give you a clear, "
        "accurate answer without wasting your time.",
    ),
    (
        "fr",
        "Je comprends votre question. Laissez-moi vérifier les détails avec attention et vous "
        "donner une réponse claire et précise.",
    ),
)


@dataclass(slots=True)
class Result:
    model: str
    steps: int
    language: str
    characters: int
    synthesis_ms: float
    audio_ms: float
    real_time_factor: float
    pcm_peak: float
    pcm_rms: float
    clipped_fraction: float
    output: str


def write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm)


def metrics(pcm: bytes) -> tuple[float, float, float]:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
    clipped = float(np.mean(np.abs(samples) >= 0.999)) if samples.size else 0.0
    return peak, rms, clipped


def run_profile(output_dir: Path, model: str, steps: int, voice: str) -> dict[str, object]:
    load_started = time.perf_counter()
    tts = TTS(model=model, auto_download=True)
    style = tts.get_voice_style(voice)
    load_ms = (time.perf_counter() - load_started) * 1_000

    # Warm the exact ONNX inference path before measuring conversational turns.
    tts.synthesize(
        "Ready.", style, total_steps=steps, speed=1.05, lang="en", silence_duration=0.18
    )
    results: list[Result] = []
    for index, (language, text) in enumerate(CASES, start=1):
        started = time.perf_counter()
        waveform, _duration = tts.synthesize(
            text,
            style,
            total_steps=steps,
            speed=1.05,
            max_chunk_length=300,
            silence_duration=0.18,
            lang=language,
        )
        synthesis_ms = (time.perf_counter() - started) * 1_000
        pcm = _waveform_to_pcm16(waveform, tts.sample_rate, SAMPLE_RATE)
        audio_ms = len(pcm) / 2 / SAMPLE_RATE * 1_000
        peak, rms, clipped = metrics(pcm)
        path = output_dir / f"{model}-steps{steps}-{index}-{language}.wav"
        write_wav(path, pcm)
        results.append(
            Result(
                model=model,
                steps=steps,
                language=language,
                characters=len(text),
                synthesis_ms=round(synthesis_ms, 2),
                audio_ms=round(audio_ms, 2),
                real_time_factor=round(synthesis_ms / audio_ms, 4),
                pcm_peak=round(peak, 4),
                pcm_rms=round(rms, 4),
                clipped_fraction=round(clipped, 6),
                output=str(path),
            )
        )
    return {
        "model": model,
        "steps": steps,
        "load_ms": round(load_ms, 2),
        "mean_synthesis_ms": round(float(np.mean([r.synthesis_ms for r in results])), 2),
        "mean_rtf": round(float(np.mean([r.real_time_factor for r in results])), 4),
        "results": [asdict(result) for result in results],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir", type=Path, default=Path("artifacts/supertonic-benchmark")
    )
    parser.add_argument("--voice", default="M1")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "machine": platform.platform(),
        "sample_rate": SAMPLE_RATE,
        "voice": args.voice,
        "profiles": [
            run_profile(args.output_dir, "supertonic-3", 8, args.voice),
            run_profile(args.output_dir, "supertonic-2", 5, args.voice),
        ],
    }
    report_path = args.output_dir / "benchmark.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
