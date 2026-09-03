#!/usr/bin/env python3
"""Generate the privacy-safe PhoneAgent Cascade baseline corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import struct
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "qualification" / "corpus" / "v1"
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    categories: tuple[str, ...]
    language: str
    samples: list[int]
    turns: list[dict[str, Any]]
    events: list[dict[str, Any]]
    description: str


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _version(command: list[str]) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    output = result.stdout or result.stderr
    return output.splitlines()[0].strip()


def _read_wav(path: Path) -> list[int]:
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError(f"unexpected generated WAV format: {path}")
        frames = wav.readframes(wav.getnframes())
    if not frames:
        raise ValueError(f"empty generated WAV: {path}")
    return list(struct.unpack(f"<{len(frames) // 2}h", frames))


def _write_wav(path: Path, samples: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _synthesize(
    temporary_root: Path,
    stem: str,
    text: str,
    *,
    voice: str,
    speed: int = 155,
) -> list[int]:
    source = temporary_root / f"{stem}-source.wav"
    normalized = temporary_root / f"{stem}-16k.wav"
    _run(["espeak-ng", "-v", voice, "-s", str(speed), "-w", str(source), text])
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(normalized),
        ]
    )
    return _read_wav(normalized)


def _duration_ms(samples: list[int]) -> int:
    return round(len(samples) * 1000 / SAMPLE_RATE)


def _event(event_type: str, at_ms: int, **details: Any) -> dict[str, Any]:
    event: dict[str, Any] = {"type": event_type, "at_ms": at_ms}
    if details:
        event["details"] = details
    return event


def _turn(text: str, end_ms: int, *, language: str, finalized: bool = True) -> dict[str, Any]:
    return {
        "role": "caller",
        "text": text,
        "language": language,
        "start_ms": 0,
        "end_ms": end_ms,
        "finalized": finalized,
    }


def _mix(primary: list[int], secondary: list[int], secondary_start_ms: int) -> list[int]:
    offset = secondary_start_ms * SAMPLE_RATE // 1000
    length = max(len(primary), offset + len(secondary))
    result = [0] * length
    for index, value in enumerate(primary):
        result[index] = value
    for index, value in enumerate(secondary):
        target = offset + index
        result[target] = max(-32768, min(32767, result[target] + value))
    return result


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _scenarios(temporary_root: Path) -> list[Scenario]:
    clear_en_text = "Hello, I would like a simple and reliable service for my family."
    clear_fr_text = "Bonjour, je cherche un service simple et fiable pour ma famille."
    tool_text = "Please schedule a callback tomorrow morning."
    long_text = (
        "I currently use several services, but reliability matters more to me than extra features. "
        "I would like one clear monthly price, no surprise conditions, and an easy setup. "
        "Please explain the closest option and what evidence you have that it will meet "
        "those needs."
    )
    teardown_text = "Thank you, that is all for today. Goodbye."

    clear_en = _synthesize(temporary_root, "clear-en", clear_en_text, voice="en-us")
    clear_fr = _synthesize(temporary_root, "clear-fr", clear_fr_text, voice="fr-fr")
    tool_call = _synthesize(temporary_root, "tool-call", tool_text, voice="en-us")
    long_turn = _synthesize(temporary_root, "long-turn", long_text, voice="en-us", speed=145)
    teardown_speech = _synthesize(
        temporary_root, "call-teardown", teardown_text, voice="en-us"
    )
    interrupting_speech = _synthesize(
        temporary_root,
        "interrupting-caller",
        "Wait, I need to change that request.",
        voice="en-us+f3",
        speed=165,
    )

    clear_en_ms = _duration_ms(clear_en)
    clear_fr_ms = _duration_ms(clear_fr)
    tool_ms = _duration_ms(tool_call)
    long_ms = _duration_ms(long_turn)
    fragment = clear_en[: SAMPLE_RATE * 650 // 1000]
    fragment_text = "Hello, I would"
    interruption = _mix(clear_en, interrupting_speech, 750)
    interruption_ms = _duration_ms(interruption)
    silence = [0] * (2 * SAMPLE_RATE)
    noise_random = random.Random(20260902)
    noise = [noise_random.randint(-900, 900) for _ in range(3 * SAMPLE_RATE)]
    teardown = teardown_speech + [0] * SAMPLE_RATE
    teardown_ms = _duration_ms(teardown)

    return [
        Scenario(
            "clear_en",
            ("clear_speech", "english"),
            "en-US",
            clear_en,
            [_turn(clear_en_text, clear_en_ms, language="en-US")],
            [
                _event("audio.started", 0),
                _event("speech.started", 0),
                _event("transcript.final", clear_en_ms - 20),
                _event("turn.committed", clear_en_ms),
            ],
            "Clear synthetic English telephone speech.",
        ),
        Scenario(
            "clear_fr",
            ("clear_speech", "french"),
            "fr-FR",
            clear_fr,
            [_turn(clear_fr_text, clear_fr_ms, language="fr-FR")],
            [
                _event("audio.started", 0),
                _event("speech.started", 0),
                _event("transcript.final", clear_fr_ms - 20),
                _event("turn.committed", clear_fr_ms),
            ],
            "Clear synthetic French telephone speech.",
        ),
        Scenario(
            "noise_only",
            ("noise",),
            "und",
            noise,
            [],
            [
                _event("audio.started", 0),
                _event("noise.detected", 250),
                _event("turn.suppressed", 2900, reason="no_authoritative_speech"),
                _event("audio.ended", 3000),
            ],
            "Seeded low-amplitude noise that must not become a caller turn.",
        ),
        Scenario(
            "interruption",
            ("interruption", "english"),
            "en-US",
            interruption,
            [
                _turn(
                    "Wait, I need to change that request.",
                    interruption_ms,
                    language="en-US",
                )
            ],
            [
                _event("assistant.playback.started", 0, generation=1),
                _event("caller.speech.started", 750),
                _event("playback.flush.requested", 760, obsolete_generation=1),
                _event("playback.flush.acknowledged", 840, obsolete_generation=1),
                _event("transcript.final", interruption_ms - 20),
                _event("turn.committed", interruption_ms),
            ],
            "Overlapping speech with an acknowledged obsolete-audio flush.",
        ),
        Scenario(
            "silence",
            ("silence",),
            "und",
            silence,
            [],
            [
                _event("silence.started", 0),
                _event("turn.suppressed", 1900, reason="silence"),
                _event("silence.ended", 2000),
            ],
            "Two seconds of digital silence.",
        ),
        Scenario(
            "fragment",
            ("fragment", "english"),
            "en-US",
            fragment,
            [_turn(fragment_text, 650, language="en-US", finalized=False)],
            [
                _event("speech.started", 0),
                _event("transcript.fragment", 620),
                _event("clarification.required", 650),
            ],
            "A deliberately truncated caller utterance.",
        ),
        Scenario(
            "tool_call",
            ("tool_call", "english"),
            "en-US",
            tool_call,
            [_turn(tool_text, tool_ms, language="en-US")],
            [
                _event("transcript.final", tool_ms - 20),
                _event("turn.committed", tool_ms),
                _event(
                    "tool.proposed",
                    tool_ms + 10,
                    tool="callback_schedule",
                    authorization="required",
                ),
            ],
            "A caller request that should propose, but not execute, a typed tool.",
        ),
        Scenario(
            "long_turn",
            ("long_turn", "english"),
            "en-US",
            long_turn,
            [_turn(long_text, long_ms, language="en-US")],
            [
                _event("speech.started", 0),
                _event("transcript.partial", 3000),
                _event("transcript.partial", 7000),
                _event("transcript.final", long_ms - 20),
                _event("turn.committed", long_ms),
            ],
            "A multi-sentence caller turn for duration and context tests.",
        ),
        Scenario(
            "call_teardown",
            ("call_teardown", "english"),
            "en-US",
            teardown,
            [_turn(teardown_text, _duration_ms(teardown_speech), language="en-US")],
            [
                _event("transcript.final", _duration_ms(teardown_speech) - 20),
                _event("turn.committed", _duration_ms(teardown_speech)),
                _event("call.teardown.started", teardown_ms - 900),
                _event("generation.flush", teardown_ms - 850),
                _event("link.closed", teardown_ms - 100, reason="clean_peer_close"),
                _event("call.teardown.completed", teardown_ms),
            ],
            "A clean goodbye and authenticated-link teardown sequence.",
        ),
    ]


def generate(output_root: Path) -> None:
    if output_root.exists():
        raise FileExistsError(f"refusing to overwrite existing corpus: {output_root}")

    output_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="phoneagent-corpus-") as temporary:
        scenarios = _scenarios(Path(temporary))

    manifest_scenarios: list[dict[str, Any]] = []
    for scenario in scenarios:
        audio_path = output_root / "audio" / f"{scenario.scenario_id}.wav"
        transcript_path = output_root / "transcripts" / f"{scenario.scenario_id}.json"
        events_path = output_root / "events" / f"{scenario.scenario_id}.json"
        _write_wav(audio_path, scenario.samples)
        _write_json(
            transcript_path,
            {
                "schema_version": 1,
                "scenario_id": scenario.scenario_id,
                "provenance": "synthetic",
                "language": scenario.language,
                "turns": scenario.turns,
            },
        )
        _write_json(
            events_path,
            {
                "schema_version": 1,
                "scenario_id": scenario.scenario_id,
                "provenance": "synthetic",
                "events": scenario.events,
            },
        )
        manifest_scenarios.append(
            {
                "id": scenario.scenario_id,
                "description": scenario.description,
                "categories": list(scenario.categories),
                "language": scenario.language,
                "duration_ms": _duration_ms(scenario.samples),
                "audio": {
                    "path": str(audio_path.relative_to(output_root)),
                    "sha256": _sha256(audio_path),
                    "encoding": "pcm_s16le",
                    "sample_rate_hz": SAMPLE_RATE,
                    "channels": 1,
                },
                "transcript": {
                    "path": str(transcript_path.relative_to(output_root)),
                    "sha256": _sha256(transcript_path),
                },
                "events": {
                    "path": str(events_path.relative_to(output_root)),
                    "sha256": _sha256(events_path),
                },
            }
        )

    manifest = {
        "schema_version": 1,
        "corpus_id": "phoneagent-cascade-baseline-v1",
        "created_at_utc": "2026-09-02T00:00:00Z",
        "coverage": [
            "clear_speech",
            "noise",
            "interruption",
            "silence",
            "fragment",
            "english",
            "french",
            "tool_call",
            "long_turn",
            "call_teardown",
        ],
        "provenance": {
            "synthetic_only": True,
            "contains_customer_data": False,
            "fixture_license": "CC0-1.0",
            "generator": "scripts/generate_baseline_corpus.py",
            "generator_dependencies": {
                "espeak_ng": _version(["espeak-ng", "--version"]),
                "ffmpeg": _version(["ffmpeg", "-version"]),
            },
        },
        "scenarios": manifest_scenarios,
    }
    _write_json(output_root / "manifest.json", manifest)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    required = ("espeak-ng", "ffmpeg")
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        raise RuntimeError(f"missing corpus generator dependencies: {', '.join(missing)}")
    generate(args.output.resolve())
    print(f"generated protected baseline corpus: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
