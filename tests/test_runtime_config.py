"""Provider selection and credential policy tests."""

from __future__ import annotations

import base64
import os
from dataclasses import replace
from pathlib import Path

import pytest

from phone_agent_gateway.ai_bridge.codex_app_server import (
    CodexAppServerLLMService,
)
from phone_agent_gateway.ai_bridge.edge_tts_service import EdgeTTSService
from phone_agent_gateway.ai_bridge.gemini_cli import GeminiCliLLMService
from phone_agent_gateway.ai_bridge.ollama_native import OllamaNativeLLMService
from phone_agent_gateway.ai_bridge.production_pipeline import (
    create_llm_service,
    create_provider_services,
    ollama_runtime_options,
)
from phone_agent_gateway.ai_bridge.runtime_config import (
    ConfigurationError,
    ProviderConfig,
    RuntimeConfig,
    load_user_secrets,
)


def test_antigravity_gemini_is_zero_credential_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHONE_AGENT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("PHONE_AGENT_LLM_MODEL", raising=False)
    config = ProviderConfig.from_env(require_credentials=False)

    assert config.llm_provider == "antigravity_gemini"
    assert config.llm_model == "gemini-3.1-flash-lite"
    assert config.speculative_pipeline_enabled is False
    assert config.conversational_reflex_enabled is False


def test_speculative_pipeline_has_one_safe_disable_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHONE_AGENT_SPECULATIVE_PIPELINE", "false")

    config = ProviderConfig.from_env(require_credentials=False)

    assert config.speculative_pipeline_enabled is False


def test_conversational_reflex_has_an_independent_disable_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHONE_AGENT_CONVERSATIONAL_REFLEX", "false")

    config = ProviderConfig.from_env(require_credentials=False)

    assert config.conversational_reflex_enabled is False


def test_sensevoice_french_falls_back_to_reliable_whisper() -> None:
    config = ProviderConfig(
        stt_provider="sensevoice",
        stt_model="iic/SenseVoiceSmall",
        stt_language="fr-FR",
    )

    services = create_provider_services(config, sample_rate=16_000)

    from phone_agent_gateway.ai_bridge.parakeet_local_stt import (
        ParakeetLocalSTTService,
    )

    assert isinstance(services.stt, ParakeetLocalSTTService)
    assert services.stt._model_id == "large-v3-turbo"


def test_whisper_turbo_uses_buffered_reliable_phone_service() -> None:
    from phone_agent_gateway.ai_bridge.parakeet_local_stt import (
        ParakeetLocalSTTService,
    )

    config = ProviderConfig(
        stt_provider="whisper_turbo",
        stt_model="large-v3-turbo",
        stt_language="en-US",
    )
    services = create_provider_services(config, sample_rate=16_000)

    assert isinstance(services.stt, ParakeetLocalSTTService)
    assert services.stt._model_id == "large-v3-turbo"


def test_ollama_provider_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHONE_AGENT_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("PHONE_AGENT_LLM_MODEL", "qwen3.5:4b-mlx")
    config = ProviderConfig.from_env(require_credentials=False)

    assert config.llm_provider == "ollama"
    assert config.llm_model == "qwen3.5:4b-mlx"
    service = create_llm_service(config)
    assert isinstance(service, OllamaNativeLLMService)
    assert config.ollama_base_url == "http://127.0.0.1:11434"
    assert config.ollama_keep_alive == "-1"
    assert config.ollama_prewarm is True
    assert config.ollama_think is False
    assert config.ollama_temperature == 0.7
    assert config.ollama_top_p == 0.8
    assert config.ollama_top_k == 20
    assert config.ollama_min_p == 0.0
    assert config.ollama_presence_penalty == 0.0
    assert config.ollama_num_ctx == 16384


def test_ollama_warmup_and_live_turns_share_the_exact_runner_shape() -> None:
    config = ProviderConfig(
        llm_provider="ollama",
        llm_model="hf.co/EryriLabs/phonellm-alpha-1-GGUF:Q4_K_M",
        ollama_num_ctx=16384,
        ollama_num_predict=96,
        ollama_temperature=0.7,
    )

    options = ollama_runtime_options(config, config.llm_model)

    assert options == {
        "temperature": 0.0,
        "top_p": 0.8,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "num_predict": 96,
        "num_ctx": 16384,
    }


def test_each_hosted_provider_requires_only_its_selected_llm_key() -> None:
    base = ProviderConfig(
        tts_provider="cartesia",
        deepgram_api_key="deepgram",
        cartesia_api_key="cartesia",
        tts_voice_id="voice",
    )
    for provider, key_field in (
        ("openai", "openai_api_key"),
        ("openrouter", "openrouter_api_key"),
        ("gemini", "google_api_key"),
    ):
        with pytest.raises(ConfigurationError):
            replace(base, llm_provider=provider).validate(require_credentials=True)
        replace(base, llm_provider=provider, **{key_field: "secret"}).validate(
            require_credentials=True
        )


def test_fully_local_speech_and_ollama_need_no_api_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHONE_AGENT_STT_PROVIDER", "whisper_mlx")
    monkeypatch.setenv("PHONE_AGENT_TTS_PROVIDER", "kokoro")
    monkeypatch.setenv("PHONE_AGENT_LLM_PROVIDER", "ollama")
    for name in ("DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "CARTESIA_VOICE_ID"):
        monkeypatch.delenv(name, raising=False)

    config = ProviderConfig.from_env(require_credentials=True)

    assert config.stt_model == "large-v3-turbo"
    assert config.tts_voice_id == "af_heart"
    assert config.tts_aggregation == "sentence"
    config.validate(require_credentials=True)


def test_edge_tts_defaults_to_safe_phrase_streaming_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHONE_AGENT_STT_PROVIDER", "whisper_mlx")
    monkeypatch.setenv("PHONE_AGENT_TTS_PROVIDER", "edge_tts")
    monkeypatch.setenv("PHONE_AGENT_LLM_PROVIDER", "ollama")
    monkeypatch.delenv("PHONE_AGENT_TTS_AGGREGATION", raising=False)
    monkeypatch.delenv("PHONE_AGENT_TTS_VOICE", raising=False)

    config = ProviderConfig.from_env(require_credentials=True)

    assert config.tts_aggregation == "phrase"
    assert config.tts_voice_id == "en-US-AndrewMultilingualNeural"
    services = create_provider_services(config, 16_000)
    assert isinstance(services.tts, EdgeTTSService)


def test_default_voice_stack_is_english_french_supertonic_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "PHONE_AGENT_STT_LANGUAGE",
        "PHONE_AGENT_TTS_PROVIDER",
        "PHONE_AGENT_TTS_MODEL",
        "PHONE_AGENT_TTS_VOICE",
        "PHONE_AGENT_TTS_AGGREGATION",
    ):
        monkeypatch.delenv(name, raising=False)

    config = ProviderConfig.from_env(require_credentials=False)

    assert config.stt_language == "en-US"
    assert config.tts_provider == "supertonic"
    # supertonic-2/steps=5 is the default because it reaches first audio in
    # ~244 ms against ~4.7 s for supertonic-3 at steps=8 on the same replies,
    # measured with the local LLM resident as it is during a real call.
    assert config.tts_model == "supertonic-2"
    assert config.tts_voice_id == "M1"
    assert config.tts_aggregation == "sentence"
    assert config.supertonic_steps == 5
    assert config.supertonic_fallback_to_edge is True


def test_realtime_defaults_are_bilingual_and_carrier_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "PHONE_AGENT_CHATGPT_TRANSCRIPTION_MODEL",
        "PHONE_AGENT_CHATGPT_INPUT_LANGUAGES",
        "PHONE_AGENT_CHATGPT_NOISE_REDUCTION",
        "PHONE_AGENT_CHATGPT_VAD_MODE",
        "PHONE_AGENT_CHATGPT_VAD_THRESHOLD",
        "PHONE_AGENT_CHATGPT_VAD_PREFIX_MS",
        "PHONE_AGENT_CHATGPT_VAD_SILENCE_MS",
        "PHONE_AGENT_CHATGPT_VAD_EAGERNESS",
        "PHONE_AGENT_CHATGPT_TRANSPORT",
        "PHONE_AGENT_CHATGPT_REASONING_EFFORT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PHONE_AGENT_PIPELINE_MODE", "s2s_chatgpt_realtime")
    with pytest.raises(ConfigurationError, match="is deprecated and removed; please migrate to 'cascade'"):
        ProviderConfig.from_env(require_credentials=False)


def test_realtime_rejects_legacy_pipeline_mode_with_migration_guidance() -> None:
    with pytest.raises(ConfigurationError, match="is deprecated and removed; please migrate to 'cascade'"):
        ProviderConfig(
            pipeline_mode="s2s_chatgpt_realtime",
            chatgpt_realtime_transport="carrier-pigeon",
        ).validate(require_credentials=False)


def test_google_tts_scene_and_sample_context_load_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHONE_AGENT_GOOGLE_TTS_SCENE", "A quiet English phone call.")
    monkeypatch.setenv(
        "PHONE_AGENT_GOOGLE_TTS_SAMPLE_CONTEXT",
        "The sales manager responds naturally to the customer's latest turn.",
    )

    config = ProviderConfig.from_env(require_credentials=False)

    assert config.google_tts_scene == "A quiet English phone call."
    assert config.google_tts_sample_context.startswith("The sales manager responds")


@pytest.mark.parametrize(
    "model",
    ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"],
)
def test_google_tts_models_available_to_studio_are_valid(model: str) -> None:
    ProviderConfig(
        tts_provider="google_genai",
        tts_model=model,
        tts_voice_id="Aoede",
    ).validate(require_credentials=False)


def test_unknown_google_tts_model_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="Gemini TTS"):
        ProviderConfig(
            tts_provider="google_genai",
            tts_model="gemini-unknown-tts",
            tts_voice_id="Aoede",
        ).validate(require_credentials=False)


def test_user_secrets_file_supplies_a_provider_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The installer rebuilds the LaunchAgent plist, so a key stored there is
    # lost on every upgrade. This file is what makes one survive.
    secrets = tmp_path / "secrets.env"
    secrets.write_text(
        "# a comment\n\nGEMINI_API_KEY = 'stored-key'\nMALFORMED\nEMPTY=\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("EMPTY", raising=False)

    assert load_user_secrets(secrets) == ["GEMINI_API_KEY"]
    assert os.environ["GEMINI_API_KEY"] == "stored-key"
    assert "EMPTY" not in os.environ


def test_a_real_environment_variable_outranks_the_secrets_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secrets = tmp_path / "secrets.env"
    secrets.write_text("GEMINI_API_KEY=stored-key\n", encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "session-key")

    assert load_user_secrets(secrets) == []
    assert os.environ["GEMINI_API_KEY"] == "session-key"


def test_a_missing_secrets_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_user_secrets(tmp_path / "absent.env") == []


def test_google_tts_without_a_key_does_not_abort_the_voice_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Antigravity is speech-to-text only, so this key has no local substitute.
    # Treating it as fatal restart-looped the voice host instead of calling;
    # create_provider_services speaks in the Edge voice instead.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    ProviderConfig(
        tts_provider="google_genai",
        tts_model="gemini-3.1-flash-tts-preview",
        tts_voice_id="Aoede",
    ).validate(require_credentials=True)


def test_supertonic_2_can_be_selected_for_speed_testing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHONE_AGENT_TTS_PROVIDER", "supertonic")
    monkeypatch.setenv("PHONE_AGENT_TTS_MODEL", "supertonic-2")
    monkeypatch.setenv("PHONE_AGENT_TTS_VOICE", "F2")
    monkeypatch.delenv("PHONE_AGENT_SUPERTONIC_STEPS", raising=False)

    config = ProviderConfig.from_env(require_credentials=True)

    assert config.tts_model == "supertonic-2"
    assert config.tts_voice_id == "F2"
    assert config.supertonic_steps == 5


def test_non_english_french_language_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="English or French"):
        ProviderConfig(stt_language="de-DE").validate(require_credentials=False)


def test_flux_preserves_final_transcripts_when_confidence_is_missing() -> None:
    config = ProviderConfig(
        stt_provider="deepgram_flux",
        deepgram_api_key="test-key",
        tts_provider="edge_tts",
        tts_voice_id="en-US-EmmaMultilingualNeural",
    )

    services = create_provider_services(config, 16_000)

    assert services.stt._settings.min_confidence == 0.0


def test_codex_app_provider_needs_no_extracted_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "phone_agent_gateway.ai_bridge.codex_app_server.resolve_codex_binary",
        lambda configured=None: "/bin/sh",
    )
    config = ProviderConfig(
        tts_provider="cartesia",
        llm_provider="codex_app",
        llm_model="gpt-5.6-luna",
        deepgram_api_key="deepgram",
        cartesia_api_key="cartesia",
        tts_voice_id="voice",
    )
    config.validate(require_credentials=True)

    service = create_llm_service(config)
    assert isinstance(service, CodexAppServerLLMService)


def test_gemini_cli_provider_needs_no_api_key() -> None:
    config = ProviderConfig(
        tts_provider="cartesia",
        llm_provider="gemini_cli",
        llm_model="gemini-2.5-flash",
        gemini_cli_binary="/usr/bin/true",
        deepgram_api_key="deepgram",
        cartesia_api_key="cartesia",
        tts_voice_id="voice",
    )
    config.validate(require_credentials=True)

    service = create_llm_service(config)
    assert isinstance(service, GeminiCliLLMService)


def test_runtime_config_expands_voice_host_lock_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "runtime" / "voice.lock"
    monkeypatch.setenv(
        "PHONE_AGENT_LINK_KEY_BASE64",
        base64.b64encode(b"x" * 32).decode(),
    )
    monkeypatch.setenv("PHONE_AGENT_VOICE_LOCK_PATH", str(lock_path))

    config = RuntimeConfig.from_env(require_provider_credentials=False)

    assert config.voice_lock_path == lock_path


def test_vibevoice_is_selectable_with_a_valid_voice() -> None:
    config = ProviderConfig(
        tts_provider="vibevoice",
        tts_model="mlx-community/VibeVoice-Realtime-0.5B-8bit",
        tts_voice_id="fr-Spk0_man",
    )
    config.validate(require_credentials=False)
    assert config.tts_provider == "vibevoice"
    assert config.vibevoice_ddpm_steps == 10


def test_vibevoice_rejects_a_voice_outside_the_shipped_cache() -> None:
    config = ProviderConfig(tts_provider="vibevoice", tts_voice_id="Emma")
    with pytest.raises(ConfigurationError, match="en-Emma_woman"):
        config.validate(require_credentials=False)


def test_vibevoice_defaults_resolve_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PHONE_AGENT_TTS_MODEL", "PHONE_AGENT_TTS_VOICE", "CARTESIA_VOICE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PHONE_AGENT_TTS_PROVIDER", "vibevoice")
    config = ProviderConfig.from_env(require_credentials=False)
    assert config.tts_model == "mlx-community/VibeVoice-Realtime-0.5B-8bit"
    assert config.tts_voice_id == "en-Emma_woman"


def test_kokoro_french_call_requires_the_french_voice() -> None:
    """An English voice reading French phonemes fails silently, so reject it."""

    config = ProviderConfig(
        tts_provider="kokoro",
        tts_model="kokoro-bf16",
        tts_voice_id="af_heart",
        stt_language="fr-FR",
    )
    with pytest.raises(ConfigurationError, match="ff_siwis"):
        config.validate(require_credentials=False)


def test_kokoro_accepts_matching_voice_and_language() -> None:
    for language, voice in (("fr-FR", "ff_siwis"), ("en-US", "af_heart"), ("en-US", "bm_george")):
        config = ProviderConfig(
            tts_provider="kokoro",
            tts_model="hexgrad/Kokoro-82M",
            tts_voice_id=voice,
            stt_language=language,
        )
        config.validate(require_credentials=False)


def test_kokoro_rejects_unsupported_model_identifier() -> None:
    config = ProviderConfig(
        tts_provider="kokoro",
        tts_model="invalid-kokoro-model-repo",
        tts_voice_id="af_heart",
        stt_language="en-US",
    )
    with pytest.raises(ConfigurationError, match="hexgrad/Kokoro-82M or kokoro-82m"):
        config.validate(require_credentials=False)


def test_kokoro_rejects_a_malformed_voice_id() -> None:
    config = ProviderConfig(
        tts_provider="kokoro",
        tts_model="hexgrad/Kokoro-82M",
        tts_voice_id="Heart",
        stt_language="en-US",
    )
    with pytest.raises(ConfigurationError, match="af_heart or ff_siwis"):
        config.validate(require_credentials=False)
