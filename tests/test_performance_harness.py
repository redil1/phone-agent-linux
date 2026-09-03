"""The Cascade performance harness must be reproducible and fail closed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from phone_agent_gateway.qualification.performance_harness import (
    DEFAULT_CORPUS,
    DEFAULT_PROFILES,
    PerformanceHarnessError,
    load_profile,
    read_fixture,
    run_benchmark,
    word_error_rate,
)


def test_profiles_register_contract_and_real_gpu_cascade() -> None:
    contract = load_profile(DEFAULT_PROFILES, "linux-x86_64-contract-ci")
    gpu = load_profile(DEFAULT_PROFILES, "linux-x86_64-rtx-a6000-local")

    assert contract["adapter"] == "deterministic_contract"
    assert gpu["adapter"] == "local_cascade"
    assert gpu["providers"]["stt"]["provider"] == "whisper_turbo"
    assert gpu["providers"]["llm"]["provider"] == "ollama"
    assert gpu["providers"]["llm"]["num_ctx"] == 16384
    assert gpu["providers"]["llm"]["turn_timeout_secs"] == 120
    assert gpu["providers"]["tts"]["provider"] == "kokoro"


def test_fixture_is_hash_verified_phone_ready_audio() -> None:
    pcm, expected, language, audio_hash = read_fixture(DEFAULT_CORPUS)

    assert pcm
    assert len(pcm) % 2 == 0
    assert expected.startswith("Hello")
    assert language == "en-US"
    assert audio_hash == "b0e01989b4d35490fe3f6a172cd913076217bea0e36a54850240a613123833bb"


@pytest.mark.parametrize(
    ("expected", "actual", "wer"),
    [
        ("a clear answer", "a clear answer", 0.0),
        ("a clear answer", "a wrong answer", 1 / 3),
        ("one two", "one two extra", 0.5),
        ("one two", "one", 0.5),
    ],
)
def test_word_error_rate_handles_substitution_insertion_and_deletion(
    expected: str, actual: str, wer: float
) -> None:
    assert word_error_rate(expected, actual) == pytest.approx(wer)


@pytest.mark.asyncio
async def test_contract_profile_measures_all_stages_and_sixty_turn_drift() -> None:
    profile = load_profile(DEFAULT_PROFILES, "linux-x86_64-contract-ci")

    report = await run_benchmark(profile, corpus=DEFAULT_CORPUS)

    assert report["status"] == "pass"
    assert report["pipeline"] == "stt_llm_tts_cascade"
    assert set(report["stages"]) == {
        "stt_final",
        "llm_ttft",
        "llm_total",
        "tts_ttfa",
        "tts_total",
        "e2e_ttfa",
        "e2e_total",
    }
    assert report["long_call"]["turns"] == 60
    assert report["long_call"]["metric"] == "llm_ttft_ms"
    assert report["long_call"]["drift_percent"] == 0.0
    assert report["cold_start_ms"] == report["prewarm_ms"]
    assert report["correctness"] == {
        "stt_all_within_wer_threshold": True,
        "llm_complete_speakable_responses": 11,
        "tts_nonempty_phone_pcm_outputs": 11,
    }
    assert all(report["checks"].values())
    assert report["contains_transcripts"] is False
    assert report["contains_audio"] is False
    assert report["contains_customer_data"] is False


@pytest.mark.asyncio
async def test_threshold_regression_is_machine_visible() -> None:
    profile = load_profile(DEFAULT_PROFILES, "linux-x86_64-contract-ci")
    profile["thresholds"]["stt_final_p95_ms"] = 0.5

    report = await run_benchmark(
        profile,
        corpus=DEFAULT_CORPUS,
        iterations=3,
        warmup_iterations=0,
        long_call_turns=4,
    )

    assert report["status"] == "threshold_failure"
    assert report["checks"]["stt_final_p95"] is False


def test_tampered_corpus_and_unknown_profile_fail_closed(tmp_path: Path) -> None:
    manifest = json.loads((DEFAULT_CORPUS / "manifest.json").read_text(encoding="utf-8"))
    manifest["scenarios"][0]["audio"]["path"] = str(DEFAULT_CORPUS / "audio" / "clear_en.wav")
    manifest["scenarios"][0]["transcript"]["path"] = str(
        DEFAULT_CORPUS / "transcripts" / "clear_en.json"
    )
    manifest["scenarios"][0]["audio"]["sha256"] = "0" * 64
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PerformanceHarnessError, match="audio hash drifted"):
        read_fixture(tmp_path)

    with pytest.raises(PerformanceHarnessError, match="unknown or duplicate"):
        load_profile(DEFAULT_PROFILES, "missing-profile")
