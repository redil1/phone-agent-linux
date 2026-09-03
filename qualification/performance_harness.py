"""Deterministic per-stage and end-to-end Cascade performance replay."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import re
import statistics
import subprocess
import time
import wave
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from pipecat.frames.frames import Frame, TTSAudioRawFrame

if TYPE_CHECKING:
    from ..ai_bridge.ollama_native import OllamaNativeClient, OllamaStreamEvent

SAMPLE_RATE = 16_000
SCHEMA_VERSION = 1
DEFAULT_PROFILES = Path(__file__).with_name("performance") / "profiles.json"
DEFAULT_CORPUS = Path(__file__).with_name("corpus") / "v1"


class PerformanceHarnessError(RuntimeError):
    """Raised when a benchmark profile or provider result is not trustworthy."""


@dataclass(frozen=True, slots=True)
class StageObservation:
    first_output_ms: float
    total_ms: float
    output_text: str = ""
    pcm_bytes: int = 0
    audio_ms: float = 0.0
    provider_metrics: dict[str, float | int | str | bool] | None = None


class CascadeAdapter(Protocol):
    async def prepare(self) -> dict[str, float]: ...

    async def stt(self, pcm: bytes, language: str) -> StageObservation: ...

    async def llm(
        self, caller_text: str, history: Sequence[dict[str, str]]
    ) -> StageObservation: ...

    async def tts(self, text: str) -> StageObservation: ...

    async def close(self) -> None: ...


class BenchmarkTTSService(Protocol):
    def run_tts(self, text: str, context_id: str) -> AsyncIterator[Frame]: ...

    async def cleanup(self) -> None: ...


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PerformanceHarnessError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PerformanceHarnessError(f"{label} must be a non-empty string")
    return value.strip()


def _number(value: object, label: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise PerformanceHarnessError(f"{label} must be a finite number")
    return float(value)


def _integer(value: object, label: str, minimum: int = 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise PerformanceHarnessError(f"{label} must be an integer >= {minimum}")
    return value


def _load_json(path: Path, label: str) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PerformanceHarnessError(f"{label} is not readable JSON: {path}") from exc
    return _object(value, label)


def load_profile(path: Path, profile_id: str) -> dict[str, object]:
    registry = _load_json(path, "performance profile registry")
    if registry.get("schema_version") != SCHEMA_VERSION:
        raise PerformanceHarnessError("unsupported performance profile schema")
    profiles = registry.get("profiles")
    if not isinstance(profiles, list):
        raise PerformanceHarnessError("performance profiles must be a list")
    matches: list[dict[str, object]] = []
    for raw in cast(list[object], profiles):
        item = _object(raw, "performance profile")
        if item.get("profile_id") == profile_id:
            matches.append(item)
    if len(matches) != 1:
        raise PerformanceHarnessError(f"unknown or duplicate performance profile: {profile_id}")
    profile = matches[0]
    if profile.get("status") != "supported":
        raise PerformanceHarnessError(f"performance profile is not supported: {profile_id}")
    return profile


def read_fixture(corpus: Path, scenario_id: str = "clear_en") -> tuple[bytes, str, str, str]:
    manifest = _load_json(corpus / "manifest.json", "performance corpus")
    scenarios = manifest.get("scenarios")
    if not isinstance(scenarios, list):
        raise PerformanceHarnessError("corpus scenarios must be a list")
    selected: list[dict[str, object]] = []
    for raw in cast(list[object], scenarios):
        item = _object(raw, "corpus scenario")
        if item.get("id") == scenario_id:
            selected.append(item)
    if len(selected) != 1:
        raise PerformanceHarnessError(f"missing or duplicate corpus scenario: {scenario_id}")
    scenario = selected[0]
    audio = _object(scenario.get("audio"), "scenario.audio")
    transcript = _object(scenario.get("transcript"), "scenario.transcript")
    audio_path = corpus / _string(audio.get("path"), "audio.path")
    transcript_path = corpus / _string(transcript.get("path"), "transcript.path")
    if _sha256(audio_path) != _string(audio.get("sha256"), "audio.sha256"):
        raise PerformanceHarnessError("corpus audio hash drifted")
    if _sha256(transcript_path) != _string(transcript.get("sha256"), "transcript.sha256"):
        raise PerformanceHarnessError("corpus transcript hash drifted")
    with wave.open(str(audio_path), "rb") as source:
        if (
            source.getframerate() != SAMPLE_RATE
            or source.getnchannels() != 1
            or source.getsampwidth() != 2
        ):
            raise PerformanceHarnessError("fixture is not phone-ready PCM16/16kHz/mono")
        pcm = source.readframes(source.getnframes())
    transcript_payload = _load_json(transcript_path, "fixture transcript")
    turns = transcript_payload.get("turns")
    if not isinstance(turns, list):
        raise PerformanceHarnessError("performance fixture must contain one authoritative turn")
    typed_turns = cast(list[object], turns)
    if len(typed_turns) != 1:
        raise PerformanceHarnessError("performance fixture must contain one authoritative turn")
    expected = _string(_object(typed_turns[0], "transcript turn").get("text"), "turn.text")
    language = _string(scenario.get("language"), "scenario.language")
    return pcm, expected, language, _sha256(audio_path)


def word_error_rate(expected: str, actual: str) -> float:
    def words(text: str) -> list[str]:
        return re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)

    reference = words(expected)
    hypothesis = words(actual)
    if not reference:
        return 0.0 if not hypothesis else 1.0
    previous = list(range(len(hypothesis) + 1))
    for index, reference_word in enumerate(reference, start=1):
        current = [index]
        for position, hypothesis_word in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[position] + 1,
                    previous[position - 1] + int(reference_word != hypothesis_word),
                )
            )
        previous = current
    return previous[-1] / len(reference)


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise PerformanceHarnessError("cannot summarize an empty measurement set")
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def summarize(values: Sequence[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "min_ms": round(min(values), 3),
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "max_ms": round(max(values), 3),
        "mean_ms": round(statistics.fmean(values), 3),
    }


class DeterministicContractAdapter:
    """Content-safe adapter proving harness semantics without claiming model latency."""

    async def prepare(self) -> dict[str, float]:
        await asyncio.sleep(0)
        return {"total_ms": 0.0}

    async def stt(self, pcm: bytes, language: str) -> StageObservation:
        await asyncio.sleep(0)
        if not pcm or not language:
            raise PerformanceHarnessError("contract STT received an invalid fixture")
        return StageObservation(1.0, 1.0, output_text=self.expected_text)

    async def llm(self, caller_text: str, history: Sequence[dict[str, str]]) -> StageObservation:
        await asyncio.sleep(0)
        if not caller_text:
            raise PerformanceHarnessError("contract LLM received empty text")
        return StageObservation(
            1.0,
            1.0,
            output_text="I can help you compare a simple, reliable option.",
            provider_metrics={"history_messages": len(history)},
        )

    async def tts(self, text: str) -> StageObservation:
        await asyncio.sleep(0)
        if not text:
            raise PerformanceHarnessError("contract TTS received empty text")
        pcm_bytes = SAMPLE_RATE * 2
        return StageObservation(1.0, 1.0, pcm_bytes=pcm_bytes, audio_ms=1000.0)

    async def close(self) -> None:
        return

    expected_text = ""


class LocalCascadeAdapter:
    """The production Whisper → Ollama → Kokoro provider boundaries."""

    def __init__(self, providers: Mapping[str, object]) -> None:
        self._stt = _object(providers.get("stt"), "providers.stt")
        self._llm = _object(providers.get("llm"), "providers.llm")
        self._tts_config = _object(providers.get("tts"), "providers.tts")
        self._client: OllamaNativeClient | None = None
        self._tts_service: BenchmarkTTSService | None = None

    async def prepare(self) -> dict[str, float]:
        from ..ai_bridge.kokoro_tts_service import prewarm_kokoro
        from ..ai_bridge.ollama_native import OllamaNativeClient
        from ..ai_bridge.parakeet_local_stt import prewarm_parakeet

        timings: dict[str, float] = {}
        started = time.perf_counter()
        timings["stt_ms"] = await asyncio.to_thread(
            prewarm_parakeet, _string(self._stt.get("model"), "stt.model")
        )
        self._client = OllamaNativeClient(
            base_url=_string(self._llm.get("base_url"), "llm.base_url"),
            turn_timeout_secs=_number(
                self._llm.get("turn_timeout_secs", 120), "llm.turn_timeout_secs"
            ),
        )
        result = await self._client.prewarm(
            model=_string(self._llm.get("model"), "llm.model"),
            keep_alive=_string(self._llm.get("keep_alive"), "llm.keep_alive"),
            options=self._llm_options(),
        )
        timings["llm_ms"] = result.elapsed_ms
        timings["tts_ms"] = await asyncio.to_thread(
            prewarm_kokoro,
            _string(self._tts_config.get("model"), "tts.model"),
            _string(self._tts_config.get("voice"), "tts.voice"),
            _string(self._tts_config.get("language"), "tts.language"),
        )
        timings["total_ms"] = (time.perf_counter() - started) * 1000
        return {key: round(value, 3) for key, value in timings.items()}

    def _llm_options(self) -> dict[str, Any]:
        return {
            "temperature": _number(self._llm.get("temperature"), "llm.temperature"),
            "num_ctx": _integer(self._llm.get("num_ctx"), "llm.num_ctx"),
            "num_predict": _integer(self._llm.get("num_predict"), "llm.num_predict"),
        }

    async def stt(self, pcm: bytes, language: str) -> StageObservation:
        from ..ai_bridge.parakeet_local_stt import transcribe_pcm_async

        started = time.perf_counter()
        result = await transcribe_pcm_async(
            pcm,
            _string(self._stt.get("model"), "stt.model"),
            language,
        )
        elapsed = (time.perf_counter() - started) * 1000
        return StageObservation(
            elapsed,
            elapsed,
            output_text=result.text,
            provider_metrics={
                "trusted_for_task": result.trusted_for_task,
                "confidence": round(result.confidence or 0.0, 4),
                "engine": str(result.diagnostics.get("engine") or "unknown"),
            },
        )

    async def llm(self, caller_text: str, history: Sequence[dict[str, str]]) -> StageObservation:
        if self._client is None:
            raise PerformanceHarnessError("local LLM was not prepared")
        started = time.perf_counter()
        first_output_ms: float | None = None
        chunks: list[str] = []
        completed: OllamaStreamEvent | None = None
        messages = [
            {
                "role": "system",
                "content": "Respond naturally in one concise sentence. Do not use tools.",
            },
            *history,
            {"role": "user", "content": caller_text},
        ]
        async for event in self._client.stream_chat(
            model=_string(self._llm.get("model"), "llm.model"),
            messages=messages,
            keep_alive=_string(self._llm.get("keep_alive"), "llm.keep_alive"),
            think=False,
            options=self._llm_options(),
        ):
            if event.content:
                if first_output_ms is None:
                    first_output_ms = (time.perf_counter() - started) * 1000
                chunks.append(event.content)
            if event.done:
                completed = event
        total_ms = (time.perf_counter() - started) * 1000
        if first_output_ms is None or not "".join(chunks).strip() or completed is None:
            raise PerformanceHarnessError("local LLM returned no complete speakable response")
        return StageObservation(
            first_output_ms,
            total_ms,
            output_text="".join(chunks).strip(),
            provider_metrics={
                "prompt_tokens": completed.prompt_tokens,
                "completion_tokens": completed.completion_tokens,
                "prompt_eval_ms": round(completed.prompt_eval_ms, 3),
                "decode_ms": round(completed.eval_ms, 3),
            },
        )

    async def tts(self, text: str) -> StageObservation:
        from ..ai_bridge.kokoro_tts_service import PhoneAgentKokoroTTSService

        if self._tts_service is None:
            self._tts_service = cast(
                BenchmarkTTSService,
                PhoneAgentKokoroTTSService(
                    model=_string(self._tts_config.get("model"), "tts.model"),
                    voice=_string(self._tts_config.get("voice"), "tts.voice"),
                    lang=_string(self._tts_config.get("language"), "tts.language"),
                    sample_rate=SAMPLE_RATE,
                ),
            )
        started = time.perf_counter()
        first_output_ms: float | None = None
        pcm_bytes = 0
        frames = self._tts_service.run_tts(text, "performance-harness")
        async for frame in frames:
            if isinstance(frame, TTSAudioRawFrame):
                if first_output_ms is None:
                    first_output_ms = (time.perf_counter() - started) * 1000
                pcm_bytes += len(frame.audio)
        total_ms = (time.perf_counter() - started) * 1000
        if first_output_ms is None or not pcm_bytes:
            raise PerformanceHarnessError("local TTS returned no phone-ready audio")
        return StageObservation(
            first_output_ms,
            total_ms,
            pcm_bytes=pcm_bytes,
            audio_ms=pcm_bytes / (SAMPLE_RATE * 2) * 1000,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
        if self._tts_service is not None:
            await self._tts_service.cleanup()


def _gpu_identity() -> dict[str, str | int] | None:
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    first = completed.stdout.splitlines()[0].split(",")
    if len(first) != 3:
        return None
    return {"name": first[0].strip(), "vram_mib": int(first[1]), "driver": first[2].strip()}


def _cpu_identity() -> str:
    identity = platform.processor().strip()
    if identity:
        return identity
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except (OSError, UnicodeDecodeError, IndexError):
        pass
    return "unknown"


def _system_memory_mib() -> int | None:
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 * 1024))
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _validate_environment(profile: Mapping[str, object]) -> dict[str, object]:
    expected = _object(profile.get("environment"), "profile.environment")
    actual: dict[str, object] = {
        "os": platform.system().lower(),
        "architecture": platform.machine().lower(),
        "python": platform.python_version(),
        "cpu": _cpu_identity(),
        "gpu": _gpu_identity(),
    }
    if (memory_mib := _system_memory_mib()) is not None:
        actual["system_memory_mib"] = memory_mib
    if _string(expected.get("os"), "environment.os") != actual["os"]:
        raise PerformanceHarnessError("host OS does not match the selected profile")
    if _string(expected.get("architecture"), "environment.architecture") != actual["architecture"]:
        raise PerformanceHarnessError("host architecture does not match the selected profile")
    accelerator = _string(expected.get("accelerator"), "environment.accelerator")
    if accelerator != "none":
        gpu = actual["gpu"]
        if gpu is None:
            raise PerformanceHarnessError("GPU does not match the selected profile")
        gpu_object = _object(gpu, "detected GPU")
        if accelerator not in _string(gpu_object.get("name"), "detected GPU name"):
            raise PerformanceHarnessError("GPU does not match the selected profile")
        minimum = _integer(expected.get("minimum_vram_mib"), "minimum_vram_mib")
        if _integer(gpu_object.get("vram_mib"), "detected GPU VRAM") < minimum:
            raise PerformanceHarnessError("GPU VRAM is below the selected profile")
    return actual


async def run_benchmark(
    profile: dict[str, object],
    *,
    corpus: Path,
    iterations: int | None = None,
    warmup_iterations: int | None = None,
    long_call_turns: int | None = None,
) -> dict[str, object]:
    profile_id = _string(profile.get("profile_id"), "profile_id")
    providers = _object(profile.get("providers"), "providers")
    pcm, expected, language, audio_hash = read_fixture(corpus)
    adapter_name = _string(profile.get("adapter"), "adapter")
    if adapter_name == "deterministic_contract":
        contract = DeterministicContractAdapter()
        contract.expected_text = expected
        adapter: CascadeAdapter = contract
    elif adapter_name == "local_cascade":
        adapter = LocalCascadeAdapter(providers)
    else:
        raise PerformanceHarnessError(f"unknown benchmark adapter: {adapter_name}")

    measured_iterations = (
        iterations if iterations is not None else _integer(profile.get("iterations"), "iterations")
    )
    warmups = (
        warmup_iterations
        if warmup_iterations is not None
        else _integer(profile.get("warmup_iterations"), "warmup_iterations", minimum=0)
    )
    drift_turns = (
        long_call_turns
        if long_call_turns is not None
        else _integer(profile.get("long_call_turns"), "long_call_turns", minimum=4)
    )
    if measured_iterations < 3 or drift_turns < 4:
        raise PerformanceHarnessError("benchmark requires >=3 samples and >=4 drift turns")
    environment = _validate_environment(profile)
    try:
        prewarm = await adapter.prepare()
        for _ in range(warmups):
            stt_warm = await adapter.stt(pcm, language)
            llm_warm = await adapter.llm(stt_warm.output_text, ())
            await adapter.tts(llm_warm.output_text)

        stt_final: list[float] = []
        llm_ttft: list[float] = []
        llm_total: list[float] = []
        tts_ttfa: list[float] = []
        tts_total: list[float] = []
        e2e_ttfa: list[float] = []
        e2e_total: list[float] = []
        tts_rtf: list[float] = []
        wers: list[float] = []
        for _ in range(measured_iterations):
            stt_result = await adapter.stt(pcm, language)
            stt_final.append(stt_result.total_ms)
            wers.append(word_error_rate(expected, stt_result.output_text))

            llm_result = await adapter.llm(expected, ())
            llm_ttft.append(llm_result.first_output_ms)
            llm_total.append(llm_result.total_ms)

            tts_result = await adapter.tts("I can help you compare a simple and reliable option.")
            tts_ttfa.append(tts_result.first_output_ms)
            tts_total.append(tts_result.total_ms)
            tts_rtf.append(tts_result.total_ms / max(tts_result.audio_ms, 0.001))

            cascade_stt = await adapter.stt(pcm, language)
            cascade_llm = await adapter.llm(cascade_stt.output_text, ())
            cascade_tts = await adapter.tts(cascade_llm.output_text)
            e2e_ttfa.append(
                cascade_stt.total_ms + cascade_llm.total_ms + cascade_tts.first_output_ms
            )
            e2e_total.append(cascade_stt.total_ms + cascade_llm.total_ms + cascade_tts.total_ms)

        history: list[dict[str, str]] = []
        drift_ttft_samples: list[float] = []
        drift_total_samples: list[float] = []
        for turn in range(drift_turns):
            prompt = f"Turn {turn + 1}: {expected}"
            result = await adapter.llm(prompt, history)
            drift_ttft_samples.append(result.first_output_ms)
            drift_total_samples.append(result.total_ms)
            history.extend(
                [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": result.output_text},
                ]
            )
        quarter = max(1, drift_turns // 4)
        # Drift is a response-onset SLO. Total completion time also depends on
        # stochastic answer length, so it is retained as diagnostic evidence
        # but must not decide whether growing conversation state stays fast.
        early = statistics.median(drift_ttft_samples[:quarter])
        late = statistics.median(drift_ttft_samples[-quarter:])
        drift_percent = ((late - early) / max(early, 0.001)) * 100

        thresholds = _object(profile.get("thresholds"), "thresholds")
        summaries: dict[str, dict[str, float | int]] = {
            "stt_final": summarize(stt_final),
            "llm_ttft": summarize(llm_ttft),
            "llm_total": summarize(llm_total),
            "tts_ttfa": summarize(tts_ttfa),
            "tts_total": summarize(tts_total),
            "e2e_ttfa": summarize(e2e_ttfa),
            "e2e_total": summarize(e2e_total),
        }
        checks = {
            "stt_final_p95": summaries["stt_final"]["p95_ms"]
            <= _number(thresholds.get("stt_final_p95_ms"), "stt threshold"),
            "llm_ttft_p95": summaries["llm_ttft"]["p95_ms"]
            <= _number(thresholds.get("llm_ttft_p95_ms"), "llm threshold"),
            "tts_ttfa_p95": summaries["tts_ttfa"]["p95_ms"]
            <= _number(thresholds.get("tts_ttfa_p95_ms"), "tts threshold"),
            "e2e_ttfa_p95": summaries["e2e_ttfa"]["p95_ms"]
            <= _number(thresholds.get("e2e_ttfa_p95_ms"), "e2e threshold"),
            "long_call_drift": drift_percent
            <= _number(thresholds.get("long_call_drift_max_percent"), "drift threshold"),
            "stt_wer": max(wers) <= _number(thresholds.get("stt_wer_max"), "WER threshold"),
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "benchmark_id": f"cascade-performance-{profile_id}",
            "status": "pass" if all(checks.values()) else "threshold_failure",
            "profile_id": profile_id,
            "adapter": adapter_name,
            "pipeline": "stt_llm_tts_cascade",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "environment": environment,
            "providers": providers,
            "fixture": {
                "corpus_id": "phoneagent-cascade-baseline-v1",
                "scenario_id": "clear_en",
                "audio_sha256": audio_hash,
                "sample_rate_hz": SAMPLE_RATE,
                "encoding": "pcm_s16le",
                "channels": 1,
                "expected_text_sha256": hashlib.sha256(expected.encode()).hexdigest(),
                "contains_customer_data": False,
            },
            "iterations": measured_iterations,
            "warmup_iterations": warmups,
            "cold_start_ms": prewarm,
            "prewarm_ms": prewarm,
            "stages": summaries,
            "tts_real_time_factor": {
                "p50": round(statistics.median(tts_rtf), 5),
                "p95": round(_percentile(tts_rtf, 0.95), 5),
            },
            "stt_word_error_rate": {
                "max": round(max(wers), 5),
                "mean": round(statistics.fmean(wers), 5),
            },
            "correctness": {
                "stt_all_within_wer_threshold": checks["stt_wer"],
                "llm_complete_speakable_responses": measured_iterations * 2 + warmups,
                "tts_nonempty_phone_pcm_outputs": measured_iterations * 2 + warmups,
            },
            "long_call": {
                "turns": drift_turns,
                "history_messages_final": len(history),
                "metric": "llm_ttft_ms",
                "early_ttft_p50_ms": round(early, 3),
                "late_ttft_p50_ms": round(late, 3),
                "drift_percent": round(drift_percent, 3),
            },
            "thresholds": thresholds,
            "checks": checks,
            "samples_ms": {
                "stt_final": [round(value, 3) for value in stt_final],
                "llm_ttft": [round(value, 3) for value in llm_ttft],
                "llm_total": [round(value, 3) for value in llm_total],
                "tts_ttfa": [round(value, 3) for value in tts_ttfa],
                "tts_total": [round(value, 3) for value in tts_total],
                "e2e_ttfa": [round(value, 3) for value in e2e_ttfa],
                "e2e_total": [round(value, 3) for value in e2e_total],
                "long_call_llm_ttft": [round(value, 3) for value in drift_ttft_samples],
                "long_call_llm_total": [round(value, 3) for value in drift_total_samples],
            },
            "contains_transcripts": False,
            "contains_audio": False,
            "contains_customer_data": False,
        }
    finally:
        await adapter.close()


async def _main_async(args: argparse.Namespace) -> int:
    profile = load_profile(args.profiles, args.profile)
    report = await run_benchmark(
        profile,
        corpus=args.corpus,
        iterations=args.iterations,
        warmup_iterations=args.warmup_iterations,
        long_call_turns=args.long_call_turns,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return int(args.require_qualified and report["status"] != "pass")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles", type=Path, default=DEFAULT_PROFILES)
    parser.add_argument("--profile", default="linux-x86_64-contract-ci")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--iterations", type=int)
    parser.add_argument("--warmup-iterations", type=int)
    parser.add_argument("--long-call-turns", type=int)
    parser.add_argument("--output", type=Path, default=Path("artifacts/performance/benchmark.json"))
    parser.add_argument("--require-qualified", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
