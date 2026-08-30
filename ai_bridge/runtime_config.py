"""Validated environment configuration for the Mac voice runtime."""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


class ConfigurationError(ValueError):
    """Required or unsafe runtime configuration."""


SECRETS_PATH = Path.home() / ".config" / "phone-agent" / "secrets.env"


def load_user_secrets(path: Path | None = None) -> list[str]:
    """Merge the operator's private key file into the environment.

    The LaunchAgent's environment is rebuilt from scratch by the installer, so a
    provider key written into the plist disappears on the next install. Keeping
    it in one mode-0600 file the runtime reads at startup survives upgrades and
    keeps the value out of the plist, the install backups, and the checkout.

    Real environment variables always win, so an operator can still override a
    stored key for a single run. Returns the names that were applied, never the
    values, so a caller can log what it picked up without leaking a secret.
    """

    secrets_path = path or SECRETS_PATH
    try:
        raw = secrets_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    applied: list[str] = []
    for line in raw.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        name, _, value = entry.partition("=")
        name = name.strip()
        value = value.strip().strip("'\"")
        if not name or not value or os.environ.get(name):
            continue
        os.environ[name] = value
        applied.append(name)
    return applied


_EDGE_PERCENT_RE = re.compile(r"^[+-]\d+%$")
_EDGE_PITCH_RE = re.compile(r"^[+-]\d+Hz$")

DEFAULT_GOOGLE_TTS_SCENE = (
    "Une conversation téléphonique individuelle en français entre Adam, directeur commercial "
    "chez OXzoon, et un client potentiel. Adam est un locuteur natif de France métropolitaine. "
    "Il parle depuis un environnement professionnel calme, avec une liaison téléphonique "
    "claire. Il est attentif, chaleureux, naturel et persuasif sans insistance. Il s'agit "
    "d'une conversation spontanée et réelle, jamais d'une publicité, d'une narration ou "
    "d'un argumentaire récité."
)
DEFAULT_GOOGLE_TTS_SAMPLE_CONTEXT = (
    "Adam vient d'écouter la dernière réponse du client et poursuit naturellement le même "
    "échange. Employer un français métropolitain contemporain, avec un rythme authentiquement "
    "français, une accentuation naturelle, des enchaînements fluides et des liaisons discrètes "
    "uniquement lorsqu'un locuteur natif les ferait. Adopter une voix chaleureuse, assurée, "
    "calme, concise et profondément humaine. Laisser le sens de chaque phrase guider "
    "l'intonation et la ponctuation guider la respiration, sans pauses mécaniques. Éviter "
    "toute prononciation influencée par l'anglais, toute surarticulation, émotion théâtrale, "
    "intonation radiophonique ou cadence robotique. Prononcer uniquement le texte fourni, "
    "sans ajouter, supprimer, traduire ni répéter de mots."
)
GOOGLE_TTS_CONTEXT_MAX_CHARS = 2_000


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _env_languages(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Parse a small, ordered ISO-639-1 language allow-list."""

    raw = os.getenv(name)
    if raw is None:
        return default
    languages: list[str] = []
    for value in raw.split(","):
        code = value.strip().lower().replace("_", "-").split("-", 1)[0]
        if not code:
            continue
        if not re.fullmatch(r"[a-z]{2}", code):
            raise ConfigurationError(f"{name} must be comma-separated ISO-639-1 codes")
        if code not in languages:
            languages.append(code)
    if not languages:
        raise ConfigurationError(f"{name} must contain at least one language")
    return tuple(languages)


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    stt_provider: str = "parakeet_local"
    stt_model: str = "mlx-community/parakeet-tdt-0.6b-v3"
    stt_language: str = "en-US"
    flux_eager_eot_threshold: float = 0.55
    flux_eot_threshold: float = 0.70
    flux_eot_timeout_ms: int = 1600
    antigravity_live_chunk_ms: int = 200
    antigravity_live_context_bias: str = ""
    antigravity_live_endpoint_ms: int = 900
    antigravity_live_incomplete_endpoint_ms: int = 1500
    antigravity_live_stability_ms: int = 280
    antigravity_live_fallback_endpoint_ms: int = 1800
    parakeet_endpoint_ms: int = 1000
    parakeet_incomplete_endpoint_ms: int = 1400
    parakeet_energy_threshold_dbfs: float = -42.0
    speculative_pipeline_enabled: bool = True
    speculative_prefetch_silence_ms: int = 180
    speculative_prefetch_stability_ms: int = 140
    speculative_fast_endpoint_ms: int = 450
    speculative_ambiguous_endpoint_ms: int = 700
    speculative_incomplete_endpoint_ms: int = 1100
    speculative_commit_wait_ms: int = 160
    conversation_repair_enabled: bool = True
    conversational_reflex_enabled: bool = True
    conversational_reflex_cooldown_ms: int = 8000
    llm_provider: str = "antigravity_gemini"
    llm_model: str = "gemini-3.1-flash-lite"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_keep_alive: str = "-1"
    ollama_prewarm: bool = True
    ollama_think: bool = False
    ollama_temperature: float = 0.7
    ollama_top_p: float = 0.8
    ollama_top_k: int = 20
    ollama_min_p: float = 0.0
    ollama_presence_penalty: float = 0.0
    ollama_num_predict: int = 192
    ollama_num_ctx: int = 8192
    ollama_turn_timeout_secs: int = 30
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Any OpenAI-compatible local server: LM Studio, vLLM, llama.cpp.
    lmstudio_base_url: str = "http://127.0.0.1:1234/v1"
    codex_binary: str = ""
    codex_reasoning_effort: str = "low"
    codex_turn_timeout_secs: int = 30
    gemini_cli_binary: str = ""
    gemini_cli_turn_timeout_secs: int = 30
    tts_provider: str = "supertonic"
    tts_model: str = "supertonic-2"
    tts_voice_id: str = "M1"
    tts_aggregation: str = "sentence"
    tts_max_buffer_delay_ms: int = 80
    google_tts_scene: str = DEFAULT_GOOGLE_TTS_SCENE
    google_tts_sample_context: str = DEFAULT_GOOGLE_TTS_SAMPLE_CONTEXT
    edge_tts_rate: str = "+0%"
    edge_tts_volume: str = "+0%"
    edge_tts_pitch: str = "+0Hz"
    edge_tts_ffmpeg_binary: str = "ffmpeg"
    edge_tts_phrase_min_chars: int = 12
    edge_tts_phrase_max_chars: int = 60
    edge_tts_connect_timeout_secs: int = 5
    edge_tts_receive_timeout_secs: int = 20
    supertonic_steps: int = 8
    supertonic_speed: float = 1.05
    supertonic_intra_op_threads: int = 0
    supertonic_inter_op_threads: int = 0
    supertonic_fallback_to_edge: bool = True
    vibevoice_ddpm_steps: int = 10
    vibevoice_cfg_scale: float = 1.3
    deepgram_api_key: str = field(default="", repr=False)
    openai_api_key: str = field(default="", repr=False)
    openrouter_api_key: str = field(default="", repr=False)
    lmstudio_api_key: str = field(default="", repr=False)
    google_api_key: str = field(default="", repr=False)
    cartesia_api_key: str = field(default="", repr=False)
    pipeline_mode: str = "cascade"
    # OpenAI recommends Marin or Cedar for the best Realtime voice quality.
    # Which channel carries the call. The two share no resources and only one
    # call runs at a time, so selecting WhatsApp cannot disturb the GSM path.
    call_channel: str = "gsm"
    whatsapp_country_code: str = "212"
    whatsapp_max_duration_secs: int = 900
    chatgpt_realtime_voice: str = "marin"
    chatgpt_realtime_model: str = "auto"
    chatgpt_realtime_transport: str = "websocket"
    chatgpt_realtime_reasoning_effort: str = "low"
    chatgpt_realtime_transcription_model: str = "gpt-live-transcribe"
    chatgpt_realtime_input_languages: tuple[str, ...] = ("en", "fr")
    chatgpt_realtime_noise_reduction: str = "off"
    chatgpt_realtime_vad_mode: str = "server_vad"
    chatgpt_realtime_vad_eagerness: str = "medium"
    chatgpt_realtime_vad_threshold: float = 0.5
    chatgpt_realtime_vad_prefix_ms: int = 300
    # A hesitant caller pausing mid-sentence was being chunked into two turns,
    # and with model-owned turn taking each chunk triggers its own reply.
    chatgpt_realtime_vad_silence_ms: int = 700
    # Re-engage the caller after a silent gap instead of waiting forever. The
    # Realtime server enforces a 5 s floor on this field. Zero disables it.
    chatgpt_realtime_idle_timeout_ms: int = 8_000
    # Playback rate for generated speech. Composition is unaffected; pacing is
    # steered from the instructions.
    # Measured on marin with the phone-grade 16 kHz render: 1.0 delivers the same
    # line at ~157-174 wpm, 1.05 at ~189. Confident sales delivery sits around
    # 165-200 wpm, and 1.10+ starts to sound clipped rather than brisk. This is
    # playback rate only; the composed pacing comes from the PACING prompt block.
    chatgpt_realtime_speed: float = 1.05

    @classmethod
    def from_env(cls, *, require_credentials: bool = True) -> ProviderConfig:
        # Every provider path is built from this method, including the child
        # call process, so the private key file is merged here rather than at
        # one entry point that the others would miss.
        load_user_secrets()
        stt_provider = os.getenv("PHONE_AGENT_STT_PROVIDER", "parakeet_local").strip().lower()
        stt_model_defaults = {
            "deepgram_flux": "flux-general-en",
            "whisper_mlx": "mlx-community/whisper-large-v3-turbo-q4",
            "antigravity_live": "google-live-bridge",
            "parakeet_local": "mlx-community/parakeet-tdt-0.6b-v3",
        }
        tts_provider = os.getenv("PHONE_AGENT_TTS_PROVIDER", "supertonic").strip().lower()
        tts_model_defaults = {
            "cartesia": "sonic-3",
            "openai": "tts-1",
            "deepgram": "aura-asteria-en",
            "edge_tts": "edge-online-neural",
            "kokoro": "kokoro-bf16",
            "google_genai": "gemini-3.1-flash-tts-preview",
            "supertonic": "supertonic-2",
            "vibevoice": "mlx-community/VibeVoice-Realtime-0.5B-8bit",
        }
        tts_voice_defaults = {
            "cartesia": "248be419-c632-4f23-add1-001000000000",
            "openai": "alloy",
            "deepgram": "aura-asteria-en",
            "edge_tts": "en-US-AndrewMultilingualNeural",
            "kokoro": "af_heart",
            "google_genai": "Algenib",
            "supertonic": "M1",
            "vibevoice": "en-Emma_woman",
        }
        tts_model = os.getenv("PHONE_AGENT_TTS_MODEL", "").strip() or tts_model_defaults.get(
            tts_provider, ""
        )
        supertonic_default_steps = 5 if tts_model == "supertonic-2" else 8
        llm_provider = os.getenv("PHONE_AGENT_LLM_PROVIDER", "antigravity_gemini").strip().lower()
        model_defaults = {
            "antigravity_gemini": "gemini-3.1-flash-lite",
            "ollama": "qwen3.5:4b-mlx",
            "openrouter": "openai/gpt-4.1",
            "openai": "gpt-4.1",
            "gemini": "gemini-2.5-flash",
            "gemini_cli": "gemini-2.5-flash",
            "codex_app": "gpt-5.6-luna",
        }
        llm_model = os.getenv("PHONE_AGENT_LLM_MODEL", "").strip() or model_defaults.get(
            llm_provider, ""
        )
        config = cls(
            stt_provider=stt_provider,
            stt_model=os.getenv("PHONE_AGENT_STT_MODEL", "").strip()
            or stt_model_defaults.get(stt_provider, ""),
            stt_language=os.getenv("PHONE_AGENT_STT_LANGUAGE", "en-US").strip(),
            flux_eager_eot_threshold=_env_float(
                "PHONE_AGENT_FLUX_EAGER_EOT_THRESHOLD", 0.55, 0.0, 1.0
            ),
            flux_eot_threshold=_env_float("PHONE_AGENT_FLUX_EOT_THRESHOLD", 0.70, 0.0, 1.0),
            flux_eot_timeout_ms=_env_int("PHONE_AGENT_FLUX_EOT_TIMEOUT_MS", 1600, 250, 5000),
            antigravity_live_chunk_ms=_env_int("PHONE_AGENT_ANTIGRAVITY_CHUNK_MS", 200, 50, 2000),
            antigravity_live_context_bias=os.getenv(
                "PHONE_AGENT_ANTIGRAVITY_CONTEXT_BIAS", ""
            ).strip(),
            antigravity_live_endpoint_ms=_env_int(
                "PHONE_AGENT_ANTIGRAVITY_ENDPOINT_MS", 900, 400, 3000
            ),
            antigravity_live_incomplete_endpoint_ms=_env_int(
                "PHONE_AGENT_ANTIGRAVITY_INCOMPLETE_ENDPOINT_MS", 1500, 600, 4000
            ),
            antigravity_live_stability_ms=_env_int(
                "PHONE_AGENT_ANTIGRAVITY_STABILITY_MS", 280, 100, 1000
            ),
            antigravity_live_fallback_endpoint_ms=_env_int(
                "PHONE_AGENT_ANTIGRAVITY_FALLBACK_ENDPOINT_MS", 1800, 700, 5000
            ),
            parakeet_endpoint_ms=_env_int("PHONE_AGENT_PARAKEET_ENDPOINT_MS", 1000, 200, 3000),
            parakeet_incomplete_endpoint_ms=_env_int(
                "PHONE_AGENT_PARAKEET_INCOMPLETE_ENDPOINT_MS", 1400, 300, 4000
            ),
            parakeet_energy_threshold_dbfs=_env_float(
                "PHONE_AGENT_PARAKEET_ENERGY_THRESHOLD_DBFS", -42.0, -80.0, -10.0
            ),
            speculative_pipeline_enabled=_env_bool("PHONE_AGENT_SPECULATIVE_PIPELINE", True),
            speculative_prefetch_silence_ms=_env_int(
                "PHONE_AGENT_SPECULATIVE_PREFETCH_SILENCE_MS", 180, 100, 1000
            ),
            speculative_prefetch_stability_ms=_env_int(
                "PHONE_AGENT_SPECULATIVE_PREFETCH_STABILITY_MS", 140, 80, 1000
            ),
            speculative_fast_endpoint_ms=_env_int(
                "PHONE_AGENT_SPECULATIVE_FAST_ENDPOINT_MS", 450, 300, 1200
            ),
            speculative_ambiguous_endpoint_ms=_env_int(
                "PHONE_AGENT_SPECULATIVE_AMBIGUOUS_ENDPOINT_MS", 700, 400, 1800
            ),
            speculative_incomplete_endpoint_ms=_env_int(
                "PHONE_AGENT_SPECULATIVE_INCOMPLETE_ENDPOINT_MS", 1100, 600, 3000
            ),
            speculative_commit_wait_ms=_env_int(
                "PHONE_AGENT_SPECULATIVE_COMMIT_WAIT_MS", 160, 0, 500
            ),
            conversation_repair_enabled=_env_bool("PHONE_AGENT_CONVERSATION_REPAIR", True),
            conversational_reflex_enabled=_env_bool("PHONE_AGENT_CONVERSATIONAL_REFLEX", True),
            conversational_reflex_cooldown_ms=_env_int(
                "PHONE_AGENT_CONVERSATIONAL_REFLEX_COOLDOWN_MS", 8000, 0, 60000
            ),
            llm_provider=llm_provider,
            llm_model=llm_model,
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip(),
            ollama_keep_alive=os.getenv("OLLAMA_KEEP_ALIVE", "-1").strip(),
            ollama_prewarm=_env_bool("PHONE_AGENT_OLLAMA_PREWARM", True),
            ollama_think=_env_bool("PHONE_AGENT_OLLAMA_THINK", False),
            ollama_temperature=_env_float("PHONE_AGENT_OLLAMA_TEMPERATURE", 0.7, 0.0, 2.0),
            ollama_top_p=_env_float("PHONE_AGENT_OLLAMA_TOP_P", 0.8, 0.0, 1.0),
            ollama_top_k=_env_int("PHONE_AGENT_OLLAMA_TOP_K", 20, 0, 1000),
            ollama_min_p=_env_float("PHONE_AGENT_OLLAMA_MIN_P", 0.0, 0.0, 1.0),
            ollama_presence_penalty=_env_float(
                "PHONE_AGENT_OLLAMA_PRESENCE_PENALTY", 0.0, -2.0, 2.0
            ),
            ollama_num_predict=_env_int("PHONE_AGENT_OLLAMA_NUM_PREDICT", 192, 16, 4096),
            ollama_num_ctx=_env_int("PHONE_AGENT_OLLAMA_NUM_CTX", 8192, 2048, 131072),
            ollama_turn_timeout_secs=_env_int("PHONE_AGENT_OLLAMA_TURN_TIMEOUT_SECS", 30, 2, 300),
            openrouter_base_url=os.getenv(
                "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            ).strip(),
            codex_binary=os.getenv("CODEX_APP_SERVER_BINARY", "").strip(),
            codex_reasoning_effort=os.getenv("CODEX_REASONING_EFFORT", "low").strip().lower(),
            codex_turn_timeout_secs=_env_int("CODEX_TURN_TIMEOUT_SECS", 30, 5, 120),
            gemini_cli_binary=os.getenv("GEMINI_CLI_BINARY", "").strip(),
            gemini_cli_turn_timeout_secs=_env_int("GEMINI_CLI_TURN_TIMEOUT_SECS", 30, 5, 120),
            tts_provider=tts_provider,
            tts_model=tts_model,
            tts_voice_id=os.getenv("PHONE_AGENT_TTS_VOICE", "").strip()
            or os.getenv("CARTESIA_VOICE_ID", "").strip()
            or tts_voice_defaults.get(tts_provider, "af_heart"),
            tts_aggregation=os.getenv(
                "PHONE_AGENT_TTS_AGGREGATION",
                {"edge_tts": "phrase", "kokoro": "sentence"}.get(tts_provider, "sentence"),
            )
            .strip()
            .lower(),
            tts_max_buffer_delay_ms=_env_int("PHONE_AGENT_TTS_MAX_BUFFER_DELAY_MS", 80, 0, 5000),
            google_tts_scene=os.getenv(
                "PHONE_AGENT_GOOGLE_TTS_SCENE", DEFAULT_GOOGLE_TTS_SCENE
            ).strip(),
            google_tts_sample_context=os.getenv(
                "PHONE_AGENT_GOOGLE_TTS_SAMPLE_CONTEXT", DEFAULT_GOOGLE_TTS_SAMPLE_CONTEXT
            ).strip(),
            edge_tts_rate=os.getenv("PHONE_AGENT_EDGE_TTS_RATE", "+0%").strip(),
            edge_tts_volume=os.getenv("PHONE_AGENT_EDGE_TTS_VOLUME", "+0%").strip(),
            edge_tts_pitch=os.getenv("PHONE_AGENT_EDGE_TTS_PITCH", "+0Hz").strip(),
            edge_tts_ffmpeg_binary=os.getenv(
                "PHONE_AGENT_EDGE_TTS_FFMPEG_BINARY", "ffmpeg"
            ).strip(),
            edge_tts_phrase_min_chars=_env_int("PHONE_AGENT_EDGE_TTS_PHRASE_MIN_CHARS", 12, 8, 120),
            edge_tts_phrase_max_chars=_env_int(
                "PHONE_AGENT_EDGE_TTS_PHRASE_MAX_CHARS", 60, 16, 240
            ),
            edge_tts_connect_timeout_secs=_env_int(
                "PHONE_AGENT_EDGE_TTS_CONNECT_TIMEOUT_SECS", 5, 1, 30
            ),
            edge_tts_receive_timeout_secs=_env_int(
                "PHONE_AGENT_EDGE_TTS_RECEIVE_TIMEOUT_SECS", 20, 2, 120
            ),
            supertonic_steps=_env_int(
                "PHONE_AGENT_SUPERTONIC_STEPS", supertonic_default_steps, 1, 100
            ),
            supertonic_speed=_env_float("PHONE_AGENT_SUPERTONIC_SPEED", 1.05, 0.7, 2.0),
            supertonic_intra_op_threads=_env_int(
                "PHONE_AGENT_SUPERTONIC_INTRA_OP_THREADS", 0, 0, 64
            ),
            supertonic_inter_op_threads=_env_int(
                "PHONE_AGENT_SUPERTONIC_INTER_OP_THREADS", 0, 0, 64
            ),
            supertonic_fallback_to_edge=_env_bool("PHONE_AGENT_SUPERTONIC_FALLBACK_TO_EDGE", True),
            vibevoice_ddpm_steps=_env_int("PHONE_AGENT_VIBEVOICE_DDPM_STEPS", 10, 1, 100),
            vibevoice_cfg_scale=_env_float("PHONE_AGENT_VIBEVOICE_CFG_SCALE", 1.3, 0.5, 5.0),
            deepgram_api_key=os.getenv("DEEPGRAM_API_KEY", "").strip(),
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            lmstudio_api_key=os.getenv("LMSTUDIO_API_KEY", "").strip(),
            lmstudio_base_url=os.getenv(
                "PHONE_AGENT_LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1"
            ).strip(),
            google_api_key=os.getenv("GOOGLE_API_KEY", "").strip()
            or os.getenv("GEMINI_API_KEY", "").strip(),
            cartesia_api_key=os.getenv("CARTESIA_API_KEY", "").strip(),
            pipeline_mode=os.getenv("PHONE_AGENT_PIPELINE_MODE", "cascade").strip().lower(),
            call_channel=os.getenv("PHONE_AGENT_CALL_CHANNEL", "gsm").strip().lower(),
            whatsapp_country_code=os.getenv("PHONE_AGENT_WHATSAPP_COUNTRY", "212").strip(),
            whatsapp_max_duration_secs=_env_int(
                "PHONE_AGENT_WHATSAPP_MAX_SECS", 900, 30, 3600
            ),
            chatgpt_realtime_voice=os.getenv("PHONE_AGENT_CHATGPT_VOICE", "marin").strip().lower(),
            chatgpt_realtime_model=os.getenv("PHONE_AGENT_CHATGPT_MODEL", "auto").strip().lower(),
            chatgpt_realtime_transport=os.getenv(
                "PHONE_AGENT_CHATGPT_TRANSPORT", "websocket"
            )
            .strip()
            .lower(),
            chatgpt_realtime_reasoning_effort=os.getenv(
                "PHONE_AGENT_CHATGPT_REASONING_EFFORT", "low"
            )
            .strip()
            .lower(),
            chatgpt_realtime_transcription_model=os.getenv(
                "PHONE_AGENT_CHATGPT_TRANSCRIPTION_MODEL", "gpt-live-transcribe"
            )
            .strip()
            .lower(),
            chatgpt_realtime_input_languages=_env_languages(
                "PHONE_AGENT_CHATGPT_INPUT_LANGUAGES", ("en", "fr")
            ),
            chatgpt_realtime_noise_reduction=os.getenv("PHONE_AGENT_CHATGPT_NOISE_REDUCTION", "off")
            .strip()
            .lower(),
            chatgpt_realtime_vad_mode=os.getenv(
                "PHONE_AGENT_CHATGPT_VAD_MODE", "server_vad"
            )
            .strip()
            .lower(),
            chatgpt_realtime_vad_eagerness=os.getenv("PHONE_AGENT_CHATGPT_VAD_EAGERNESS", "medium")
            .strip()
            .lower(),
            chatgpt_realtime_vad_threshold=_env_float(
                "PHONE_AGENT_CHATGPT_VAD_THRESHOLD", 0.5, 0.0, 1.0
            ),
            chatgpt_realtime_vad_prefix_ms=_env_int(
                "PHONE_AGENT_CHATGPT_VAD_PREFIX_MS", 300, 0, 5_000
            ),
            chatgpt_realtime_vad_silence_ms=_env_int(
                "PHONE_AGENT_CHATGPT_VAD_SILENCE_MS", 700, 100, 5_000
            ),
            chatgpt_realtime_idle_timeout_ms=_env_int(
                "PHONE_AGENT_CHATGPT_IDLE_TIMEOUT_MS", 8_000, 0, 60_000
            ),
            chatgpt_realtime_speed=_env_float("PHONE_AGENT_CHATGPT_SPEED", 1.05, 0.8, 2.0),
        )
        config.validate(require_credentials=require_credentials)
        return config

    def validate(self, *, require_credentials: bool) -> None:
        # Which channel carries the call is independent of how speech is
        # produced, so this is checked for cascade too. Nested under the
        # Realtime branch it silently accepted anything in cascade mode.
        if self.call_channel not in {"gsm", "whatsapp", "whatsapp_phone"}:
            raise ValueError(
                "call channel must be 'gsm', 'whatsapp' or 'whatsapp_phone', "
                f"got {self.call_channel!r}"
            )
        if self.pipeline_mode not in {"cascade", "s2s_chatgpt_realtime"}:
            raise ConfigurationError(
                "PHONE_AGENT_PIPELINE_MODE must be cascade or s2s_chatgpt_realtime"
            )
        if self.pipeline_mode == "s2s_chatgpt_realtime":
            if self.chatgpt_realtime_transport not in {"websocket", "webrtc"}:
                raise ConfigurationError(
                    "PHONE_AGENT_CHATGPT_TRANSPORT must be websocket or webrtc"
                )
            if self.chatgpt_realtime_reasoning_effort not in {
                "minimal",
                "low",
                "medium",
                "high",
                "xhigh",
            }:
                raise ConfigurationError(
                    "PHONE_AGENT_CHATGPT_REASONING_EFFORT must be minimal, low, medium, "
                    "high, or xhigh"
                )
            allowed_voices = {
                "alloy",
                "ash",
                "ballad",
                "cedar",
                "coral",
                "echo",
                "marin",
                "sage",
                "shimmer",
                "verse",
            }
            if self.chatgpt_realtime_voice not in allowed_voices:
                raise ConfigurationError(
                    f"PHONE_AGENT_CHATGPT_VOICE must be one of {sorted(allowed_voices)}"
                )
            if not self.stt_language.lower().startswith(("en", "fr")):
                raise ConfigurationError(
                    "PHONE_AGENT_STT_LANGUAGE must be an English or French locale"
                )
            if self.chatgpt_realtime_transcription_model not in {
                "gpt-live-transcribe",
                "gpt-transcribe",
                "gpt-4o-mini-transcribe",
                "gpt-4o-transcribe",
            }:
                raise ConfigurationError("PHONE_AGENT_CHATGPT_TRANSCRIPTION_MODEL is not supported")
            if len(self.chatgpt_realtime_input_languages) > 1 and (
                self.chatgpt_realtime_transcription_model
                not in {"gpt-live-transcribe", "gpt-transcribe"}
            ):
                raise ConfigurationError(
                    "Bilingual Realtime input requires gpt-live-transcribe or gpt-transcribe"
                )
            if not set(self.chatgpt_realtime_input_languages) <= {"en", "fr"}:
                raise ConfigurationError(
                    "PHONE_AGENT_CHATGPT_INPUT_LANGUAGES currently supports only en and fr"
                )
            if self.chatgpt_realtime_noise_reduction not in {
                "off",
                "near_field",
                "far_field",
            }:
                raise ConfigurationError(
                    "PHONE_AGENT_CHATGPT_NOISE_REDUCTION must be off, near_field, or far_field"
                )
            if self.chatgpt_realtime_vad_mode not in {"server_vad", "semantic_vad"}:
                raise ConfigurationError(
                    "PHONE_AGENT_CHATGPT_VAD_MODE must be server_vad or semantic_vad"
                )
            if self.chatgpt_realtime_vad_eagerness not in {
                "low",
                "medium",
                "high",
                "auto",
            }:
                raise ConfigurationError(
                    "PHONE_AGENT_CHATGPT_VAD_EAGERNESS must be low, medium, high, or auto"
                )
            # The Realtime server rejects the whole session.update below 5000,
            # which would fail the call at connect time rather than degrade.
            if self.chatgpt_realtime_idle_timeout_ms and (
                self.chatgpt_realtime_idle_timeout_ms < 5_000
            ):
                raise ConfigurationError(
                    "PHONE_AGENT_CHATGPT_IDLE_TIMEOUT_MS must be 0 or at least 5000"
                )
            if not 0.8 <= self.chatgpt_realtime_speed <= 2.0:
                raise ConfigurationError(
                    "PHONE_AGENT_CHATGPT_SPEED must be between 0.8 and 2.0"
                )
            return
        supported = {
            "stt": (
                self.stt_provider,
                {"antigravity_live", "deepgram_flux", "parakeet_local", "whisper_mlx"},
            ),
            "llm": (
                self.llm_provider,
                {
                    "antigravity_gemini",
                    "codex_app",
                    "gemini",
                    "gemini_cli",
                    "ollama",
                    "openai",
                    "openrouter",
                    "lmstudio",
                },
            ),
            "tts": (
                self.tts_provider,
                {
                    "cartesia",
                    "edge_tts",
                    "google_genai",
                    "kokoro",
                    "supertonic",
                    "vibevoice",
                },
            ),
        }
        for role, (selected, allowed) in supported.items():
            if selected not in allowed:
                raise ConfigurationError(
                    f"unsupported {role} provider {selected!r}; supported: {sorted(allowed)}"
                )
        if not self.stt_language.lower().startswith(("en", "fr")):
            raise ConfigurationError("PHONE_AGENT_STT_LANGUAGE must be an English or French locale")
        if self.tts_aggregation not in {"phrase", "sentence", "token"}:
            raise ConfigurationError(
                "PHONE_AGENT_TTS_AGGREGATION must be 'phrase', 'sentence', or 'token'"
            )
        if self.tts_aggregation == "phrase" and self.tts_provider != "edge_tts":
            raise ConfigurationError(
                "PHONE_AGENT_TTS_AGGREGATION=phrase is currently supported only by edge_tts"
            )
        if self.tts_provider == "google_genai" and self.tts_model not in {
            "gemini-3.1-flash-tts-preview",
            "gemini-2.5-flash-preview-tts",
        }:
            raise ConfigurationError(
                "PHONE_AGENT_TTS_MODEL must be gemini-3.1-flash-tts-preview or "
                "gemini-2.5-flash-preview-tts for Google Gemini TTS"
            )
        if len(self.google_tts_scene) > GOOGLE_TTS_CONTEXT_MAX_CHARS:
            raise ConfigurationError(
                f"PHONE_AGENT_GOOGLE_TTS_SCENE must be at most "
                f"{GOOGLE_TTS_CONTEXT_MAX_CHARS} characters"
            )
        if len(self.google_tts_sample_context) > GOOGLE_TTS_CONTEXT_MAX_CHARS:
            raise ConfigurationError(
                f"PHONE_AGENT_GOOGLE_TTS_SAMPLE_CONTEXT must be at most "
                f"{GOOGLE_TTS_CONTEXT_MAX_CHARS} characters"
            )
        if self.tts_provider == "supertonic":
            if self.tts_model not in {"supertonic-2", "supertonic-3"}:
                raise ConfigurationError(
                    "PHONE_AGENT_TTS_MODEL must be supertonic-2 or supertonic-3"
                )
            if not re.fullmatch(r"[MF][1-5]", self.tts_voice_id):
                raise ConfigurationError("PHONE_AGENT_TTS_VOICE must be M1-M5 or F1-F5")
        if self.tts_provider == "kokoro":
            # Kokoro now runs on MLX, so the model names the quantization of an
            # mlx-community repo rather than an ONNX file. bf16 measured fastest
            # on this hardware; 4bit trades ~1.5x of that for less memory.
            if self.tts_model not in {"kokoro-bf16", "kokoro-4bit"}:
                raise ConfigurationError(
                    "PHONE_AGENT_TTS_MODEL for kokoro must be kokoro-bf16 or kokoro-4bit"
                )
            # A Kokoro voice encodes its own language in the prefix, and the
            # phonemizer is driven separately by the call language. Mismatching
            # them produces an English voice reading French phonemes rather than
            # any visible error, so reject the combination up front.
            if not re.fullmatch(r"[abefhijpz][fm]_[a-z]+", self.tts_voice_id):
                raise ConfigurationError(
                    "PHONE_AGENT_TTS_VOICE for kokoro must be a voice id such as "
                    "af_heart or ff_siwis"
                )
            french_call = self.stt_language.lower().startswith("fr")
            french_voice = self.tts_voice_id.startswith("ff_")
            if french_call != french_voice:
                raise ConfigurationError(
                    "Kokoro voice and call language must match: use ff_siwis for a French "
                    f"call or an af_/am_/bf_/bm_ voice for English (language="
                    f"{self.stt_language!r}, voice={self.tts_voice_id!r})"
                )
        if self.tts_provider == "vibevoice" and not re.fullmatch(
            r"[a-z]{2}-[A-Za-z0-9_]+", self.tts_voice_id
        ):
            raise ConfigurationError(
                "PHONE_AGENT_TTS_VOICE for vibevoice must look like en-Emma_woman or fr-Spk0_man"
            )
        if not _EDGE_PERCENT_RE.fullmatch(self.edge_tts_rate):
            raise ConfigurationError("PHONE_AGENT_EDGE_TTS_RATE must look like +0% or -10%")
        if not _EDGE_PERCENT_RE.fullmatch(self.edge_tts_volume):
            raise ConfigurationError("PHONE_AGENT_EDGE_TTS_VOLUME must look like +0% or -10%")
        if not _EDGE_PITCH_RE.fullmatch(self.edge_tts_pitch):
            raise ConfigurationError("PHONE_AGENT_EDGE_TTS_PITCH must look like +0Hz or -10Hz")
        if not self.edge_tts_ffmpeg_binary:
            raise ConfigurationError("PHONE_AGENT_EDGE_TTS_FFMPEG_BINARY cannot be empty")
        if self.edge_tts_phrase_min_chars > self.edge_tts_phrase_max_chars:
            raise ConfigurationError(
                "PHONE_AGENT_EDGE_TTS_PHRASE_MIN_CHARS cannot exceed "
                "PHONE_AGENT_EDGE_TTS_PHRASE_MAX_CHARS"
            )
        if self.parakeet_incomplete_endpoint_ms < self.parakeet_endpoint_ms:
            raise ConfigurationError(
                "PHONE_AGENT_PARAKEET_INCOMPLETE_ENDPOINT_MS cannot be lower than "
                "PHONE_AGENT_PARAKEET_ENDPOINT_MS"
            )
        if self.antigravity_live_incomplete_endpoint_ms < self.antigravity_live_endpoint_ms:
            raise ConfigurationError(
                "PHONE_AGENT_ANTIGRAVITY_INCOMPLETE_ENDPOINT_MS cannot be lower than "
                "PHONE_AGENT_ANTIGRAVITY_ENDPOINT_MS"
            )
        if self.antigravity_live_fallback_endpoint_ms < self.antigravity_live_endpoint_ms:
            raise ConfigurationError(
                "PHONE_AGENT_ANTIGRAVITY_FALLBACK_ENDPOINT_MS cannot be lower than "
                "PHONE_AGENT_ANTIGRAVITY_ENDPOINT_MS"
            )
        if self.speculative_ambiguous_endpoint_ms < self.speculative_fast_endpoint_ms:
            raise ConfigurationError(
                "PHONE_AGENT_SPECULATIVE_AMBIGUOUS_ENDPOINT_MS cannot be lower than "
                "PHONE_AGENT_SPECULATIVE_FAST_ENDPOINT_MS"
            )
        if self.speculative_incomplete_endpoint_ms < self.speculative_ambiguous_endpoint_ms:
            raise ConfigurationError(
                "PHONE_AGENT_SPECULATIVE_INCOMPLETE_ENDPOINT_MS cannot be lower than "
                "PHONE_AGENT_SPECULATIVE_AMBIGUOUS_ENDPOINT_MS"
            )
        if self.codex_reasoning_effort not in {"low", "medium", "high", "xhigh"}:
            raise ConfigurationError("CODEX_REASONING_EFFORT must be low, medium, high, or xhigh")
        if not self.ollama_keep_alive or len(self.ollama_keep_alive) > 32:
            raise ConfigurationError("OLLAMA_KEEP_ALIVE must be 1 to 32 characters")
        if not require_credentials:
            return
        missing = []
        if self.stt_provider == "deepgram_flux" and not self.deepgram_api_key:
            missing.append("DEEPGRAM_API_KEY")
        if self.llm_provider == "openai" and not self.openai_api_key:
            missing.append("OPENAI_API_KEY")
        if self.llm_provider == "openrouter" and not self.openrouter_api_key:
            missing.append("OPENROUTER_API_KEY")
        if self.llm_provider == "gemini" and not self.google_api_key:
            missing.append("GOOGLE_API_KEY")
        if self.tts_provider == "cartesia" and not self.cartesia_api_key:
            missing.append("CARTESIA_API_KEY")
        if self.tts_provider == "cartesia" and not self.tts_voice_id:
            missing.append("CARTESIA_VOICE_ID")
        # Google Gemini TTS is deliberately absent from this list. It is the only
        # provider with a working free substitute, and treating its missing key
        # as fatal killed the voice host on every restart instead of placing the
        # call. create_provider_services falls back to the Edge voice and says so.
        if missing:
            raise ConfigurationError(
                f"missing production provider configuration: {', '.join(missing)}"
            )


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    device_id: str | None
    control_host: str
    control_port: int
    protocol_control_port: int
    rx_port: int
    tx_port: int
    sample_rate: int
    frame_ms: int
    input_queue_frames: int
    auto_answer: bool
    record_calls: bool
    memory_enabled: bool
    task_id: str
    event_stream_enabled: bool
    voice_lock_path: Path
    system_prompt: str
    link_authentication_key: bytes | None = field(repr=False)
    providers: ProviderConfig = field(repr=False)

    @classmethod
    def from_env(cls, *, require_provider_credentials: bool = True) -> RuntimeConfig:
        raw_key = os.getenv("PHONE_AGENT_LINK_KEY_BASE64", "").strip()
        key_path = os.getenv("PHONE_AGENT_LINK_KEY_FILE", "").strip()
        if not raw_key and not key_path:
            default_key = Path.home() / ".config" / "phone-agent" / "link.key"
            if default_key.is_file():
                key_path = str(default_key)
        link_key: bytes | None = None
        if raw_key and key_path:
            raise ConfigurationError(
                "configure only one of PHONE_AGENT_LINK_KEY_BASE64 or PHONE_AGENT_LINK_KEY_FILE"
            )
        if raw_key:
            try:
                link_key = base64.b64decode(raw_key, validate=True)
            except ValueError as exc:
                raise ConfigurationError("PHONE_AGENT_LINK_KEY_BASE64 is invalid") from exc
            if len(link_key) < 32:
                raise ConfigurationError("PHONE_AGENT_LINK_KEY_BASE64 must decode to >= 32 bytes")
        elif key_path:
            try:
                link_key = Path(key_path).expanduser().read_bytes()
            except OSError as exc:
                raise ConfigurationError("PHONE_AGENT_LINK_KEY_FILE could not be read") from exc
            if not 32 <= len(link_key) <= 4096:
                raise ConfigurationError(
                    "PHONE_AGENT_LINK_KEY_FILE must contain between 32 and 4096 bytes"
                )

        return cls(
            device_id=os.getenv("PHONE_AGENT_DEVICE_ID") or None,
            control_host=os.getenv("PHONE_AGENT_CONTROL_HOST", "127.0.0.1"),
            control_port=_env_int("PHONE_AGENT_CONTROL_PORT", 8765, 1, 65535),
            protocol_control_port=_env_int("PHONE_AGENT_PROTOCOL_CONTROL_PORT", 8768, 1, 65535),
            rx_port=_env_int("PHONE_AGENT_RX_PORT", 8766, 1, 65535),
            tx_port=_env_int("PHONE_AGENT_TX_PORT", 8767, 1, 65535),
            sample_rate=_env_int("PHONE_AGENT_SAMPLE_RATE", 16_000, 8_000, 48_000),
            frame_ms=_env_int("PHONE_AGENT_FRAME_MS", 20, 10, 40),
            input_queue_frames=_env_int("PHONE_AGENT_INPUT_QUEUE_FRAMES", 25, 2, 50),
            auto_answer=_env_bool("PHONE_AGENT_AUTO_ANSWER", False),
            record_calls=_env_bool("PHONE_AGENT_RECORD_CALLS", False),
            memory_enabled=_env_bool("PHONE_AGENT_MEMORY_ENABLED", True),
            task_id=os.getenv("PHONE_AGENT_TASK_ID", "iptv_subscription_sales").strip(),
            event_stream_enabled=_env_bool("PHONE_AGENT_EVENT_STREAM", False),
            voice_lock_path=Path(
                os.getenv(
                    "PHONE_AGENT_VOICE_LOCK_PATH",
                    str(Path.home() / ".local" / "share" / "phone-agent" / "voice-host.lock"),
                )
            ).expanduser(),
            system_prompt=os.getenv(
                "PHONE_AGENT_SYSTEM_PROMPT",
                (
                    "You are a helpful and polite AI voice assistant on a telephone call. "
                    "Speak only English or French. Use English by default and switch to French "
                    "when the caller requests it or speaks a complete French sentence. Keep your "
                    "responses concise, warm, and conversational (1 to 2 short spoken sentences). "
                    "Never use markdown, emojis, asterisks, or bullet points."
                ),
            ).strip(),
            link_authentication_key=link_key,
            providers=ProviderConfig.from_env(require_credentials=require_provider_credentials),
        )

    @property
    def pipeline_mode(self) -> str:
        return self.providers.pipeline_mode

    @property
    def call_channel(self) -> str:
        return self.providers.call_channel

    @property
    def whatsapp_country_code(self) -> str:
        return self.providers.whatsapp_country_code

    @property
    def whatsapp_max_duration_secs(self) -> int:
        return self.providers.whatsapp_max_duration_secs
