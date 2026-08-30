"""Production Pipecat cascade for one cellular call."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from typing import Any

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.observers.loggers.metrics_log_observer import MetricsLogObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContext,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.services.tts_service import TextAggregationMode
from pipecat.transcriptions.language import Language
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from .agent_policy import (
    AgentPolicyRuntime,
    EventSink,
    PlaybackEventProcessor,
    ResponsePolicyProcessor,
    TranscriptionPolicyProcessor,
)
from .cascade_tools import (
    CascadeToolRuntime,
    NativeToolBinding,
    ToolCallProcessor,
    emitted_tool_instructions,
    llm_supports_native_tools,
)
from .conversational_reflex import ConversationalReflexProcessor
from .pipecat_transport import PhoneAgentTransport
from .repair_processor import ConversationRepairProcessor
from .runtime_config import ProviderConfig, RuntimeConfig
from .speculative_turn import SpeculativeTurnCoordinator
from .telemetry import CallTelemetry, FluxTurnTimingTracker
from .turn_continuity import SemanticTurnGuardProcessor

logger = logging.getLogger("PhoneAgentPipeline")

DEFAULT_ENGLISH_STT_CONTEXT = (
    "Natural telephone conversation primarily in English. Transcribe only words actually "
    "spoken by the caller and never translate them. Preserve names, numbers, dates, addresses, "
    "and complete sentences. If the caller audibly switches and speaks a complete French "
    "sentence, preserve that sentence in French. Never infer a language switch from noise, "
    "isolated shared words, or an uncertain partial hypothesis."
)
DEFAULT_FRENCH_STT_CONTEXT = (
    "Conversation téléphonique naturelle principalement en français. Transcrire uniquement "
    "les mots réellement prononcés sans les traduire. Conserver les noms, nombres, dates, "
    "adresses et phrases complètes. Si l'appelant passe clairement à l'anglais et prononce une "
    "phrase anglaise complète, conserver cette phrase en anglais. Ne jamais déduire un changement "
    "de langue à partir du bruit ou d'une hypothèse partielle incertaine."
)


def _default_stt_context(language: str) -> str:
    return (
        DEFAULT_FRENCH_STT_CONTEXT
        if language.lower().startswith("fr")
        else DEFAULT_ENGLISH_STT_CONTEXT
    )


@dataclass(slots=True)
class ProviderServices:
    stt: Any
    llm: Any
    tts: Any


def _language(value: str) -> Language:
    try:
        return Language(value)
    except ValueError as exc:
        raise ValueError(f"unsupported Pipecat language code: {value!r}") from exc


def create_provider_services(config: ProviderConfig, sample_rate: int) -> ProviderServices:
    """Build the pinned low-latency provider cascade.

    Construction is deliberately separate from pipeline assembly so later A/B
    tests can inject a second provider stack without changing call ownership.
    """

    if config.stt_provider == "deepgram_flux":
        flux_settings: dict[str, Any] = {
            "model": config.stt_model,
            "eager_eot_threshold": config.flux_eager_eot_threshold,
            "eot_threshold": config.flux_eot_threshold,
            "eot_timeout_ms": config.flux_eot_timeout_ms,
            # Never silently delete a completed turn. A missing/below-threshold
            # confidence previously produced no TranscriptionFrame and could
            # leave the caller waiting for the five-second turn watchdog.
            "min_confidence": 0.0,
        }
        if config.stt_model == "flux-general-multi":
            flux_settings["language_hints"] = [_language(config.stt_language)]

        stt = DeepgramFluxSTTService(
            api_key=config.deepgram_api_key,
            sample_rate=sample_rate,
            should_interrupt=True,
            settings=DeepgramFluxSTTService.Settings(**flux_settings),
        )
    elif config.stt_provider == "whisper_mlx":
        from pipecat.services.whisper.stt import WhisperSTTServiceMLX

        stt = WhisperSTTServiceMLX(
            sample_rate=sample_rate,
            settings=WhisperSTTServiceMLX.Settings(
                model=config.stt_model,
                language=_language(config.stt_language),
                no_speech_prob=0.6,
                temperature=0.0,
                engine="mlx",
            ),
        )
    elif config.stt_provider == "antigravity_live":
        from .antigravity_live_stt import AntigravityLiveSTTService

        context_bias = config.antigravity_live_context_bias
        if not context_bias:
            context_bias = _default_stt_context(config.stt_language)
        stt = AntigravityLiveSTTService(
            sample_rate=sample_rate,
            language=config.stt_language,
            chunk_duration_ms=config.antigravity_live_chunk_ms,
            context_bias=context_bias,
            silence_endpoint_ms=config.antigravity_live_endpoint_ms,
            incomplete_endpoint_ms=config.antigravity_live_incomplete_endpoint_ms,
            transcript_stability_ms=config.antigravity_live_stability_ms,
            fallback_endpoint_ms=config.antigravity_live_fallback_endpoint_ms,
            speculative_pipeline_enabled=config.speculative_pipeline_enabled,
            speculative_prefetch_silence_ms=config.speculative_prefetch_silence_ms,
            speculative_prefetch_stability_ms=config.speculative_prefetch_stability_ms,
            speculative_fast_endpoint_ms=config.speculative_fast_endpoint_ms,
            speculative_ambiguous_endpoint_ms=config.speculative_ambiguous_endpoint_ms,
            speculative_incomplete_endpoint_ms=config.speculative_incomplete_endpoint_ms,
        )
    elif config.stt_provider == "parakeet_local":
        from .parakeet_local_stt import ParakeetLocalSTTService

        stt = ParakeetLocalSTTService(
            sample_rate=sample_rate,
            language=config.stt_language,
            model=config.stt_model,
            endpoint_ms=config.parakeet_endpoint_ms,
            incomplete_endpoint_ms=config.parakeet_incomplete_endpoint_ms,
            prefetch_silence_ms=config.speculative_prefetch_silence_ms,
            energy_threshold_dbfs=config.parakeet_energy_threshold_dbfs,
            speculative_pipeline_enabled=config.speculative_pipeline_enabled,
        )
    else:
        raise ValueError(f"unsupported STT provider: {config.stt_provider}")

    llm = create_llm_service(config)

    if config.tts_provider == "supertonic":
        from .edge_tts_service import EdgeTTSService
        from .supertonic_tts_service import PhoneAgentSupertonicTTSService

        fallback = None
        if config.supertonic_fallback_to_edge:
            fallback = EdgeTTSService(
                sample_rate=sample_rate,
                voice="en-US-AndrewMultilingualNeural",
                rate=config.edge_tts_rate,
                volume=config.edge_tts_volume,
                pitch=config.edge_tts_pitch,
                ffmpeg_binary=config.edge_tts_ffmpeg_binary,
                connect_timeout_secs=config.edge_tts_connect_timeout_secs,
                receive_timeout_secs=config.edge_tts_receive_timeout_secs,
                text_aggregation_mode=TextAggregationMode.SENTENCE,
                phrase_aggregation=False,
            )
        tts = PhoneAgentSupertonicTTSService(
            model=config.tts_model,
            voice=config.tts_voice_id,
            language=config.stt_language,
            steps=config.supertonic_steps,
            speed=config.supertonic_speed,
            sample_rate=sample_rate,
            intra_op_threads=config.supertonic_intra_op_threads,
            inter_op_threads=config.supertonic_inter_op_threads,
            fallback_renderer=fallback,
        )
    elif config.tts_provider == "cartesia":
        aggregation = TextAggregationMode(config.tts_aggregation)
        tts = CartesiaTTSService(
            api_key=config.cartesia_api_key,
            sample_rate=sample_rate,
            text_aggregation_mode=aggregation,
            max_buffer_delay_ms=config.tts_max_buffer_delay_ms,
            settings=CartesiaTTSService.Settings(
                model=config.tts_model,
                voice=config.tts_voice_id,
                language=_language(config.stt_language),
            ),
        )
    elif config.tts_provider == "edge_tts":
        from .edge_tts_service import EdgeTTSService

        tts = EdgeTTSService(
            sample_rate=sample_rate,
            voice=config.tts_voice_id,
            rate=config.edge_tts_rate,
            volume=config.edge_tts_volume,
            pitch=config.edge_tts_pitch,
            ffmpeg_binary=config.edge_tts_ffmpeg_binary,
            connect_timeout_secs=config.edge_tts_connect_timeout_secs,
            receive_timeout_secs=config.edge_tts_receive_timeout_secs,
            text_aggregation_mode=TextAggregationMode.SENTENCE,
            phrase_aggregation=config.tts_aggregation == "phrase",
            phrase_min_chars=config.edge_tts_phrase_min_chars,
            phrase_max_chars=config.edge_tts_phrase_max_chars,
        )
    elif config.tts_provider == "kokoro":
        from .kokoro_tts_service import PhoneAgentKokoroTTSService

        tts = PhoneAgentKokoroTTSService(
            sample_rate=sample_rate,
            model=config.tts_model,
            voice=config.tts_voice_id or "af_heart",
            lang=config.stt_language,
        )
    elif config.tts_provider == "google_genai":
        google_tts_key = config.google_api_key or os.getenv("GEMINI_API_KEY", "")
        if not google_tts_key:
            # The Antigravity bridge cannot supply this one. It is speech-to-text
            # only: its whole audio surface is StreamAudioTranscription /
            # SendAudioChunk / EndAudioSession, and the language server links no
            # synthesis code at all. So an absent key means this voice genuinely
            # cannot run, and refusing outright used to abort the voice host in a
            # restart loop rather than place the call. Speak in the free Edge
            # voice instead, exactly as local synthesis already falls back.
            from .edge_tts_service import EdgeTTSService

            logger.warning(
                "Google Gemini TTS needs GEMINI_API_KEY and Antigravity cannot provide "
                "speech synthesis; falling back to the Edge voice for this call"
            )
            tts = EdgeTTSService(
                sample_rate=sample_rate,
                voice="en-US-AndrewMultilingualNeural",
                rate=config.edge_tts_rate,
                volume=config.edge_tts_volume,
                pitch=config.edge_tts_pitch,
                ffmpeg_binary=config.edge_tts_ffmpeg_binary,
                connect_timeout_secs=config.edge_tts_connect_timeout_secs,
                receive_timeout_secs=config.edge_tts_receive_timeout_secs,
                text_aggregation_mode=TextAggregationMode.SENTENCE,
                phrase_aggregation=False,
            )
        else:
            from .google_genai_tts_service import GoogleGenAITTSService

            tts = GoogleGenAITTSService(
                api_key=google_tts_key,
                model=config.tts_model or "gemini-3.1-flash-tts-preview",
                voice=config.tts_voice_id or "Aoede",
                language=config.stt_language,
                scene=config.google_tts_scene,
                sample_context=config.google_tts_sample_context,
                sample_rate=sample_rate,
                text_aggregation_mode=TextAggregationMode.SENTENCE,
            )
    elif config.tts_provider == "vibevoice":
        from .vibevoice_tts_service import VibeVoiceTTSService

        tts = VibeVoiceTTSService(
            sample_rate=sample_rate,
            model=config.tts_model,
            voice=config.tts_voice_id,
            language=config.stt_language,
            ddpm_steps=config.vibevoice_ddpm_steps,
            cfg_scale=config.vibevoice_cfg_scale,
            text_aggregation_mode=TextAggregationMode.SENTENCE,
        )
    else:
        raise ValueError(f"unsupported TTS provider: {config.tts_provider}")
    return ProviderServices(stt=stt, llm=llm, tts=tts)


def create_llm_service(config: ProviderConfig) -> Any:
    """Create one of the supported LLMs without changing pipeline topology."""

    settings = {"model": config.llm_model, "temperature": 0.4}
    if config.llm_provider == "antigravity_gemini":
        from .antigravity_gemini_llm import AntigravityGeminiLLMService

        return AntigravityGeminiLLMService(
            model=config.llm_model,
            system_instruction="",
            temperature=0.4,
            speculative_commit_wait_ms=config.speculative_commit_wait_ms,
            fallback_model="qwen3.5:4b-mlx",
            fallback_base_url=config.ollama_base_url,
        )
    if config.llm_provider == "codex_app":
        from .codex_app_server import CodexAppServerLLMService

        return CodexAppServerLLMService(
            model=config.llm_model,
            system_instruction=(
                "You are a highly capable AI speaking on a telephone call. "
                "Be natural, concise, attentive, and honest."
            ),
            reasoning_effort=config.codex_reasoning_effort,
            binary=config.codex_binary or None,
            turn_timeout_secs=config.codex_turn_timeout_secs,
        )
    if config.llm_provider == "gemini_cli":
        from .gemini_cli import GeminiCliLLMService

        return GeminiCliLLMService(
            model=config.llm_model,
            system_instruction=(
                "You are a highly capable AI speaking on a telephone call. "
                "Be natural, concise, attentive, and honest."
            ),
            binary=config.gemini_cli_binary or None,
            turn_timeout_secs=config.gemini_cli_turn_timeout_secs,
        )
    if config.llm_provider == "ollama":
        from .ollama_native import OllamaNativeLLMService

        return OllamaNativeLLMService(
            model=config.llm_model,
            base_url=config.ollama_base_url,
            temperature=config.ollama_temperature,
            top_p=config.ollama_top_p,
            top_k=config.ollama_top_k,
            min_p=config.ollama_min_p,
            presence_penalty=config.ollama_presence_penalty,
            num_predict=config.ollama_num_predict,
            num_ctx=config.ollama_num_ctx,
            think=config.ollama_think,
            keep_alive=config.ollama_keep_alive,
            turn_timeout_secs=config.ollama_turn_timeout_secs,
            prewarm_on_start=config.ollama_prewarm,
        )
    if config.llm_provider == "openrouter":
        from pipecat.services.openrouter.llm import OpenRouterLLMService

        return OpenRouterLLMService(
            api_key=config.openrouter_api_key,
            base_url=config.openrouter_base_url,
            settings=OpenRouterLLMService.Settings(**settings),
        )
    if config.llm_provider == "openai":
        from pipecat.services.openai.llm import OpenAILLMService

        return OpenAILLMService(
            api_key=config.openai_api_key,
            settings=OpenAILLMService.Settings(**settings),
        )
    if config.llm_provider == "gemini":
        from pipecat.services.google.llm import GoogleLLMService

        return GoogleLLMService(
            api_key=config.google_api_key,
            settings=GoogleLLMService.Settings(**settings),
        )
    if config.llm_provider == "lmstudio":
        from pipecat.services.openai.llm import OpenAILLMService

        # LM Studio, vLLM, llama.cpp's server and most other local runners speak
        # the OpenAI chat API. One provider therefore covers all of them, and
        # they get real function calling rather than the text protocol.
        return OpenAILLMService(
            api_key=config.lmstudio_api_key or "lm-studio",
            base_url=config.lmstudio_base_url,
            settings=OpenAILLMService.Settings(**settings),
        )
    raise ValueError(f"unsupported LLM provider: {config.llm_provider}")


async def prewarm_primary_llm(config: ProviderConfig) -> float | None:
    """Make the selected local LLM resident before the host accepts calls."""

    if config.llm_provider != "ollama":
        return None
    from .ollama_native import OllamaNativeClient

    client = OllamaNativeClient(
        base_url=config.ollama_base_url,
        turn_timeout_secs=config.ollama_turn_timeout_secs,
    )
    try:
        result = await client.prewarm(
            model="qwen3.5:4b-mlx",
            keep_alive=config.ollama_keep_alive,
            options={
                "temperature": config.ollama_temperature,
                "top_p": config.ollama_top_p,
                "top_k": config.ollama_top_k,
                "min_p": config.ollama_min_p,
                "presence_penalty": config.ollama_presence_penalty,
                "num_predict": config.ollama_num_predict,
                "num_ctx": config.ollama_num_ctx,
            },
        )
        return result.elapsed_ms
    finally:
        await client.close()


async def prewarm_speech_models(config: ProviderConfig) -> dict[str, float]:
    """Load lazy local speech weights before the first caller utterance."""

    timings: dict[str, float] = {}
    if config.stt_provider == "whisper_mlx":
        import time

        import mlx_whisper
        import numpy as np

        started = time.perf_counter()
        silence = np.zeros(3_200, dtype=np.float32)
        await asyncio.to_thread(
            mlx_whisper.transcribe,
            silence,
            path_or_hf_repo=config.stt_model,
            temperature=0.0,
            language=config.stt_language,
            no_speech_threshold=0.6,
            verbose=None,
        )
    if config.stt_provider == "parakeet_local":
        from .parakeet_local_stt import prewarm_parakeet

        timings["parakeet_ms"] = await asyncio.to_thread(prewarm_parakeet, config.stt_model)
    if config.tts_provider == "vibevoice":
        from .vibevoice_tts_service import prewarm_vibevoice

        # Weights are large and the first load has been observed to take
        # minutes, so this must never happen on the first caller turn.
        timings["vibevoice_ms"] = await asyncio.to_thread(
            prewarm_vibevoice,
            model_id=config.tts_model,
            voice=config.tts_voice_id,
            sample_rate=16_000,
        )
    if config.tts_provider == "kokoro":
        from .kokoro_tts_service import prewarm_kokoro

        # Loading the weights is not enough: the first synthesis also compiles
        # the Metal kernels, and that cost belongs before the call, not on the
        # greeting.
        timings["kokoro_ms"] = await asyncio.to_thread(
            prewarm_kokoro,
            config.tts_model,
            config.tts_voice_id or "af_heart",
            config.stt_language,
        )
    if config.tts_provider == "supertonic":
        import time

        from .supertonic_tts_service import prewarm_supertonic

        started = time.perf_counter()
        try:
            await asyncio.to_thread(
                prewarm_supertonic,
                model=config.tts_model,
                voice=config.tts_voice_id,
                language=config.stt_language,
                steps=config.supertonic_steps,
                speed=config.supertonic_speed,
                sample_rate=16_000,
                intra_op_threads=config.supertonic_intra_op_threads,
                inter_op_threads=config.supertonic_inter_op_threads,
            )
            timings["supertonic_ms"] = (time.perf_counter() - started) * 1000
        except Exception:
            if not config.supertonic_fallback_to_edge:
                raise
            logger.exception("Supertonic prewarm failed; calls will use the Edge fallback")
    return timings


class ProductionCallPipeline:
    """Own the Pipecat worker and providers for exactly one phone call."""

    def __init__(
        self,
        transport: PhoneAgentTransport,
        config: RuntimeConfig,
        *,
        services: ProviderServices | None = None,
        caller_id: str = "anonymous",
        call_direction: str = "outbound",
        event_sink: EventSink | None = None,
    ) -> None:
        self.transport = transport
        self.config = config
        self.services = services or create_provider_services(config.providers, config.sample_rate)
        self.telemetry = CallTelemetry()
        self.policy = AgentPolicyRuntime(
            caller_id=caller_id,
            task_id=config.task_id,
            language=config.providers.stt_language,
            call_direction=call_direction,
            additional_instructions=config.system_prompt,
            memory_enabled=config.memory_enabled,
            event_sink=event_sink,
        )
        self.context = LLMContext()
        self.context.add_message({"role": "system", "content": self.policy.system_prompt})
        self.policy.attach_context(self.context)
        # The cascade previously ran with no tools at all, so an agent could
        # offer a customer a CRM lookup or a WhatsApp message it had no way to
        # perform. The runtime is built here and attached in start(), because
        # its backends have to be reached before the first caller turn.
        self.tools = CascadeToolRuntime(
            policy=self.policy,
            caller_id=caller_id,
            call_id=str(transport.session.call_id),
            system_prompt=config.system_prompt,
            event_sink=event_sink,
        )
        self.tool_processor: ToolCallProcessor | None = None
        self._native_tools = llm_supports_native_tools(self.services.llm)
        self.semantic_turn_guard = SemanticTurnGuardProcessor()
        self.transcription_policy = TranscriptionPolicyProcessor(self.policy)
        self.conversation_repair = ConversationRepairProcessor(
            self.policy, enabled=config.providers.conversation_repair_enabled
        )
        self.response_policy = ResponsePolicyProcessor(self.policy)
        self.playback_events = PlaybackEventProcessor(self.policy, transport.session)
        self.speculative_turn: SpeculativeTurnCoordinator | None = None
        if config.providers.speculative_pipeline_enabled:
            speculative_turn = SpeculativeTurnCoordinator(
                context=self.context,
                llm=self.services.llm,
                tts=self.services.tts,
                policy=self.policy,
                event_sink=event_sink,
            )
            if speculative_turn.supported and hasattr(
                self.services.stt, "set_speculation_handlers"
            ):
                self.speculative_turn = speculative_turn
                self.services.stt.set_speculation_handlers(
                    speculative_turn.consider,
                    speculative_turn.cancel,
                )
                logger.info("speculative turn pipeline enabled")
            else:
                logger.warning(
                    "speculative turn pipeline unsupported by selected providers; using normal path"
                )
        uses_external_turn_frames = config.providers.stt_provider in {
            "antigravity_live",
            "deepgram_flux",
            "parakeet_local",
        }
        self.user_aggregator, self.assistant_aggregator = LLMContextAggregatorPair(
            self.context,
            user_params=LLMUserAggregatorParams(
                # Flux publishes authoritative start/end frames and explicitly
                # requests external turn strategies. Configure that up front so
                # Pipecat does not load and then discard Local Smart Turn.
                user_turn_strategies=(
                    ExternalUserTurnStrategies() if uses_external_turn_frames else None
                ),
                vad_analyzer=None if uses_external_turn_frames else SileroVADAnalyzer(),
            ),
        )
        self.conversational_reflex = ConversationalReflexProcessor(
            tts=self.services.tts,
            language=config.providers.stt_language,
            enabled=config.providers.conversational_reflex_enabled,
            sample_rate=config.sample_rate,
            cooldown_ms=config.providers.conversational_reflex_cooldown_ms,
            event_sink=event_sink,
        )
        if config.providers.conversational_reflex_enabled:
            if self.conversational_reflex.supported:
                logger.info("context-safe conversational reflexes enabled")
            else:
                logger.warning(
                    "conversational reflexes unsupported by selected TTS; using normal path"
                )
        if not self._native_tools:
            # Upstream of the response policy on purpose: that processor releases
            # sentences to speech, so an unparsed tool block reaching it would be
            # read aloud to the caller as raw JSON.
            self.tool_processor = ToolCallProcessor(
                self.tools,
                context=self.context,
                llm=self.services.llm,
                preamble=self._speak_tool_preamble,
            )
        self.pipeline = Pipeline(
            [
                element
                for element in (
                transport.input(),
                self.services.stt,
                self.semantic_turn_guard,
                self.transcription_policy,
                self.conversation_repair,
                self.user_aggregator,
                self.conversational_reflex,
                self.services.llm,
                self.tool_processor,
                self.response_policy,
                self.services.tts,
                transport.output(),
                self.playback_events,
                self.assistant_aggregator,
                )
                if element is not None
            ]
        )
        self.worker = PipelineWorker(
            self.pipeline,
            conversation_id=str(transport.session.call_id),
            params=PipelineParams(
                audio_in_sample_rate=config.sample_rate,
                audio_out_sample_rate=config.sample_rate,
                enable_metrics=True,
                enable_usage_metrics=True,
                report_only_initial_ttfb=False,
                start_metadata={"call_id": str(transport.session.call_id)},
            ),
            observers=[MetricsLogObserver(), *self.telemetry.observers],
            enable_turn_tracking=False,
            enable_rtvi=False,
            idle_timeout_secs=300,
        )
        self.runner: WorkerRunner | None = None
        self._runner_task: asyncio.Task | None = None
        self._started = asyncio.Event()
        self.flux_turn_tracker: FluxTurnTimingTracker | None = None
        self._policy_closed = False
        self._greeted = False
        self._greet_lock = asyncio.Lock()
        self._background_warm: asyncio.Task | None = None
        self._register_handlers()

    def _register_handlers(self) -> None:
        if isinstance(self.services.stt, DeepgramFluxSTTService):
            self.flux_turn_tracker = FluxTurnTimingTracker(self.telemetry)
            self.flux_turn_tracker.bind(self.services.stt)

        @self.worker.event_handler("on_pipeline_started")
        async def on_pipeline_started(_worker, _frame) -> None:
            self._started.set()
            logger.info("voice pipeline started call_id=%s", self.transport.session.call_id)

        @self.worker.event_handler("on_pipeline_error")
        async def on_pipeline_error(_worker, frame) -> None:
            logger.error(
                "voice pipeline error call_id=%s frame=%s",
                self.transport.session.call_id,
                frame,
            )

    async def _speak_tool_preamble(self, name: str) -> None:
        """Tell the caller something is happening before a tool runs.

        A tool call costs a second model pass, so without this the caller hears
        several seconds of nothing and starts saying "hello?", which the turn
        logic then treats as a new question.
        """

        french = self.config.providers.stt_language.lower().startswith("fr")
        line = "Un instant, je vérifie." if french else "One moment, let me check that."
        with contextlib.suppress(Exception):
            await self.worker.queue_frame(TTSSpeakFrame(line, append_to_context=False))

    async def _attach_tools(self) -> None:
        """Make the catalog reachable by whichever protocol this model speaks."""

        try:
            catalog = await self.tools.start()
        except Exception:
            # A tool backend that will not start must not cost the call itself.
            logger.exception("cascade tools could not start; continuing without them")
            return
        if not catalog:
            logger.warning("cascade tool catalog is empty for task=%s", self.config.task_id)
            return
        # The persona was compiled before any backend was reached, so it still
        # says the agent has no tools. Rebuild it and replace the system message
        # in place, or the model will decline to use what it now holds.
        refreshed = self.policy.recompile_system_prompt()
        for message in self.context.messages:
            if isinstance(message, dict) and message.get("role") == "system":
                message["content"] = refreshed
                break
        if self._native_tools:
            bound = NativeToolBinding(self.tools, self.services.llm, self.context).bind()
            logger.info("bound %d cascade tools natively", bound)
            return
        # A service that accepts definitions gets the real ones and answers with
        # structured calls; the processor downstream sees the same block either
        # way. This is the hook any future provider implements to opt in.
        publish = getattr(self.services.llm, "set_tool_definitions", None)
        if callable(publish):
            publish(self.tools.definitions)
            logger.info(
                "published %d cascade tools to the model's own tool protocol",
                len(catalog),
            )
            return
        instructions = emitted_tool_instructions(self.tools)
        if instructions:
            self.context.add_message({"role": "system", "content": instructions})
        logger.info(
            "exposed %d cascade tools through the emitted protocol", len(catalog)
        )

    async def start(self, timeout_secs: float = 20.0) -> None:
        if self._runner_task is not None:
            return
        await self._attach_tools()
        self.runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await self.runner.add_workers(self.worker)
        self._runner_task = asyncio.create_task(
            self.runner.run(), name=f"voice-call-{self.transport.session.call_id}"
        )
        try:
            await asyncio.wait_for(self._started.wait(), timeout=timeout_secs)
        except Exception:
            await self.cancel("pipeline startup failed")
            raise

    async def warm_llm_prefix(self) -> None:
        """Populate the model's prompt cache with this call's system prefix."""

        services = getattr(self, "services", None)
        warm = getattr(getattr(services, "llm", None), "warm_prompt_prefix", None)
        if not callable(warm):
            return
        try:
            elapsed = await warm(self.policy.system_prompt)
            if elapsed is not None:
                logger.info("LLM prompt prefix warmed elapsed_ms=%.1f", elapsed)
        except Exception:
            # A cold cache only costs latency on the first turn; never let it
            # take down the call.
            logger.warning("LLM prompt prefix warm failed", exc_info=True)

    async def greet(self) -> None:
        """Start with a persona-conditioned opening greeting."""
        # Runs against the greeting's own synthesis and playback, which take
        # seconds, so the first caller turn finds the prefix already cached.
        warm_task = asyncio.create_task(self.warm_llm_prefix(), name="llm-prefix-warm")
        self._background_warm = warm_task
        async with self._greet_lock:
            if self._greeted:
                return
            self._greeted = True
            compiler = self.policy.persona_compiler
            identity = getattr(
                compiler,
                "effective_identity",
                compiler.persona_data.get("identity", {}),
            )
            name = identity.get("name", "Adam AI")
            role = str(identity.get("role", ""))
            language = self.config.providers.stt_language.lower()
            task_contract = getattr(self.policy, "task_contract", {})
            openings = task_contract.get("opening_greeting", {})
            opening_key = "fr" if language.startswith("fr") else "en"
            configured_greeting = str(openings.get(opening_key, "")).strip()
            greeting = self.policy.call_context.opening_greeting(
                name=str(name),
                role=role,
                language=language,
                configured_outbound=configured_greeting,
            )
            spoken, _evaluation = await self.policy.finalize_response(
                greeting,
                response_kind="greeting",
            )
            if spoken:
                await self.worker.queue_frame(TTSSpeakFrame(spoken, append_to_context=True))

    async def stop(self, timeout_secs: float = 10.0) -> None:
        task = self._runner_task
        if task is None:
            return
        await self.worker.stop_when_done()
        try:
            await asyncio.wait_for(task, timeout=timeout_secs)
        except TimeoutError:
            await self.cancel("graceful call shutdown timed out")
        finally:
            self._runner_task = None
            await self._close_policy()

    async def cancel(self, reason: str) -> None:
        if self.runner is not None:
            await self.runner.cancel(reason)
        task = self._runner_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await self._close_policy()

    async def _close_policy(self) -> None:
        if self._policy_closed:
            return
        self._policy_closed = True
        warm = self._background_warm
        self._background_warm = None
        if warm is not None and not warm.done():
            warm.cancel()
            await asyncio.gather(warm, return_exceptions=True)
        if self.speculative_turn is not None:
            await self.speculative_turn.close()
        await self.tools.close()
        await self.policy.close()
