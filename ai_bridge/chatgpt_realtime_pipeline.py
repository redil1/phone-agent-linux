"""Production ChatGPT Realtime WebRTC Speech-to-Speech pipeline for cellular calls.

Directly bridges the phone's 16 kHz cellular downlink/uplink with OpenAI's
Realtime Voice Media Gateway over WebRTC, bypassing discrete STT -> LLM -> TTS
cascades while preserving caller memory, task contracts, and interruption flushing.
"""

from __future__ import annotations

import asyncio
import fractions
import inspect
import json
import logging
import math
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import quote

import av
import numpy as np
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from curl_cffi.requests import AsyncSession
from pipecat.frames.frames import OutputAudioRawFrame

from .agent_policy import AgentPolicyRuntime, EventSink
from .chatgpt_realtime_auth import ChatGPTAuthManager
from .conversation_repair import TurnQuality, normalize, words_of
from .frappe_integration import FrappeConfigStore, FrappeToolRuntime
from .mcp_broker import McpToolBroker
from .openwa_integration import OpenWAConfigStore, OpenWAToolRuntime
from .pipecat_transport import AudioWriteResult, PhoneAgentTransport
from .runtime_config import RuntimeConfig
from .tasks.tool_catalog import (
    END_CALL_TOOL_NAME,
    build_end_call_tool,
    build_tool_catalog,
    execute_tool,
    tool_definitions,
)
from .tool_argument_grounding import ground_tool_arguments
from .tool_control import ManagedToolRuntime, ToolControlStore
from .web_research import WebResearchConfigStore, WebResearchToolRuntime

logger = logging.getLogger("ChatGPTRealtimePipeline")

WEB_RTC_SAMPLE_RATE = 48000
PHONE_SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_DURATION = 0.02  # 20ms
SAMPLES_PER_FRAME_48K = int(WEB_RTC_SAMPLE_RATE * FRAME_DURATION)  # 960 samples
PHONE_CHUNK_BYTES = 640  # 320 samples @ 16kHz mono 16-bit
REMOTE_AUDIO_QUEUE_FRAMES = 2000  # 40 seconds; RTP receive must not wait on phone playout.
# Realtime control events and RTP travel independently. Once OpenAI confirms its
# output buffer stopped, leave a small fixed ordering window for final RTP packets
# that were already in flight. Never infer completion from silence inside speech.
RTP_SETTLE_AFTER_OUTPUT_STOP_SECS = 0.25
REMOTE_AUDIO_DRAIN_TIMEOUT_SECS = 20.0
BARGE_IN_CONFIRM_SECS = 0.60
CALLER_TURN_SETTLE_SECS = 0.35
CALLER_ACTIVITY_MIN_RMS = 70.0
CALLER_ACTIVITY_OPEN_RATIO = 2.8
CALLER_ACTIVITY_CLOSE_RATIO = 1.8
CALLER_ACTIVITY_HANGOVER_FRAMES = 15
LOW_TRANSCRIPTION_CONFIDENCE = 0.18

CallCompletionSink = Callable[[str], Awaitable[None] | None]


class _ResponsePhase(StrEnum):
    """Authoritative lifecycle for the one response allowed in the default conversation."""

    IDLE = "idle"
    CREATING = "creating"
    IN_PROGRESS = "in_progress"
    CANCELLING = "cancelling"


@dataclass(frozen=True, slots=True)
class _PhoneAudioQueueItem:
    response_key: str
    pcm: bytes | None = None


@dataclass(slots=True)
class _RealtimeResponseState:
    key: str
    kind: str
    text: str = ""
    interrupted: bool = False
    playback_closed: bool = False
    rendered_at_start: int = -1
    remote_audio_frames_at_start: int = 0
    audio_end: tuple[int, int] | None = None
    audio_done_received: bool = False
    audio_finish_started: bool = False
    output_buffer_stopped: bool = False
    output_buffer_terminal: asyncio.Event = field(default_factory=asyncio.Event)
    output_item_id: str | None = None
    output_content_index: int = 0
    monitor_started: bool = False
    finalized: asyncio.Event = field(default_factory=asyncio.Event)


class PhoneMediaStreamTrack(MediaStreamTrack):
    """Feeds phone downlink audio into the WebRTC peer connection.

    Pumps 16 kHz mono PCM frames from the phone gateway, resamples them to 48 kHz,
    and yields standard 20ms av.AudioFrame packets to aiortc.
    """

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._loop_thread_id = threading.get_ident()
        self._pts = 0
        self._time_base = fractions.Fraction(1, WEB_RTC_SAMPLE_RATE)
        self._running = True
        # A new Realtime session starts with generic server defaults. Do not let
        # the caller's initial "hello" reach VAD until our exact persona is
        # acknowledged and the outbound opening has been queued.
        self._input_enabled = False
        self._first_audio_logged = False
        # This detector is used only to confirm sustained barge-in. It never
        # modifies the audio: the old fixed gate erased quiet word beginnings
        # before either Realtime VAD or the speech model could hear them.
        self._speech_active = False
        self._speech_hangover = 0
        self._noise_floor_rms = 20.0
        self._audio_frames = 0
        self._speech_like_frames = 0
        self._silent_frames = 0
        self._queue_dropped_frames = 0
        self._clipped_samples = 0
        self._total_samples = 0
        self._rms_sum = 0.0
        self._peak_sample = 0
        self._resampler = av.AudioResampler(
            format="s16",
            layout="mono",
            rate=WEB_RTC_SAMPLE_RATE,
        )

    def push_pcm_frame(self, pcm_bytes: bytes) -> None:
        """Accept one 16kHz mono PCM chunk (thread-safe)."""
        if not self._running or not pcm_bytes:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if threading.get_ident() == self._loop_thread_id:
            self._offer_pcm_frame(pcm_bytes)
            return
        try:
            loop.call_soon_threadsafe(self._offer_pcm_frame, pcm_bytes)
        except RuntimeError:
            pass

    def _offer_pcm_frame(self, pcm_bytes: bytes) -> None:
        if not self._running or not self._input_enabled:
            return
        if not self._first_audio_logged:
            self._first_audio_logged = True
            logger.info(
                "PhoneMediaStreamTrack received first phone audio chunk (%d bytes)",
                len(pcm_bytes),
            )
        try:
            if self._queue.full():
                try:
                    self._queue.get_nowait()
                    self._queue_dropped_frames += 1
                except asyncio.QueueEmpty:
                    pass
            self._queue.put_nowait(pcm_bytes)
        except Exception:
            pass

    def enable_input(self) -> None:
        """Open caller audio only after persona binding and greeting dispatch."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._input_enabled = True
        logger.info("Realtime caller audio gate opened after outbound greeting dispatch")

    @property
    def speech_active(self) -> bool:
        """Whether caller energy indicates sustained speech, without filtering it."""

        return self._speech_active

    def _observe_audio_level(self, samples: np.ndarray) -> None:
        """Track privacy-safe input quality and a non-destructive activity estimate."""

        if samples.size == 0:
            return
        values = samples.astype(np.float64)
        rms = float(np.sqrt(np.mean(values * values)))
        peak = int(np.max(np.abs(values)))
        self._audio_frames += 1
        self._total_samples += int(samples.size)
        self._rms_sum += rms
        self._peak_sample = max(self._peak_sample, peak)
        self._clipped_samples += int(np.count_nonzero(np.abs(values) >= 32760.0))
        if rms < 8.0:
            self._silent_frames += 1

        open_threshold = max(
            CALLER_ACTIVITY_MIN_RMS,
            self._noise_floor_rms * CALLER_ACTIVITY_OPEN_RATIO,
        )
        close_threshold = max(
            CALLER_ACTIVITY_MIN_RMS * 0.65,
            self._noise_floor_rms * CALLER_ACTIVITY_CLOSE_RATIO,
        )
        if self._speech_active:
            if rms >= close_threshold:
                self._speech_hangover = CALLER_ACTIVITY_HANGOVER_FRAMES
            elif self._speech_hangover > 0:
                self._speech_hangover -= 1
            else:
                self._speech_active = False
        elif rms >= open_threshold:
            self._speech_active = True
            self._speech_hangover = CALLER_ACTIVITY_HANGOVER_FRAMES

        if self._speech_active:
            self._speech_like_frames += 1
        elif rms <= max(CALLER_ACTIVITY_MIN_RMS, self._noise_floor_rms * 1.5):
            # Learn only while no speech is active so quiet voices never become
            # part of the estimated noise floor.
            self._noise_floor_rms = 0.98 * self._noise_floor_rms + 0.02 * rms

    def quality_snapshot(self) -> dict[str, int | float]:
        """Return caller-input diagnostics without retaining any speech audio."""

        frames = max(1, self._audio_frames)
        samples = max(1, self._total_samples)
        return {
            "caller_input_frames": self._audio_frames,
            "caller_input_queue_drops": self._queue_dropped_frames,
            "caller_input_mean_rms": round(self._rms_sum / frames, 1),
            "caller_input_peak": self._peak_sample,
            "caller_input_speech_frame_pct": round(self._speech_like_frames * 100.0 / frames, 1),
            "caller_input_silence_frame_pct": round(self._silent_frames * 100.0 / frames, 1),
            "caller_input_clipped_sample_pct": round(self._clipped_samples * 100.0 / samples, 4),
            "caller_input_noise_floor_rms": round(self._noise_floor_rms, 1),
        }

    async def recv(self) -> av.AudioFrame:
        """Yield the next 48kHz audio frame to aiortc."""
        if not self._running or not self._input_enabled:
            # Yield silence if stopped
            await asyncio.sleep(FRAME_DURATION)
            silence = np.zeros(SAMPLES_PER_FRAME_48K, dtype=np.int16)
            frame = av.AudioFrame.from_ndarray(silence.reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = WEB_RTC_SAMPLE_RATE
            frame.pts = self._pts
            frame.time_base = self._time_base
            self._pts += SAMPLES_PER_FRAME_48K
            return frame

        try:
            pcm_16k = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            await asyncio.sleep(FRAME_DURATION)
            pcm_16k = b"\x00" * PHONE_CHUNK_BYTES

        samples_16k = np.frombuffer(pcm_16k, dtype=np.int16)
        if len(samples_16k) == 0:
            samples_48k = np.zeros(SAMPLES_PER_FRAME_48K, dtype=np.int16)
        else:
            self._observe_audio_level(samples_16k)
            input_frame = av.AudioFrame.from_ndarray(
                samples_16k.reshape(1, -1),
                format="s16",
                layout="mono",
            )
            input_frame.sample_rate = PHONE_SAMPLE_RATE
            converted = self._resampler.resample(input_frame)
            converted_arrays = [
                frame.to_ndarray().reshape(-1).astype(np.int16, copy=False) for frame in converted
            ]
            samples_48k = (
                np.concatenate(converted_arrays)
                if converted_arrays
                else np.zeros(0, dtype=np.int16)
            )
            if len(samples_48k) < SAMPLES_PER_FRAME_48K:
                samples_48k = np.pad(
                    samples_48k,
                    (0, SAMPLES_PER_FRAME_48K - len(samples_48k)),
                )
            elif len(samples_48k) > SAMPLES_PER_FRAME_48K:
                samples_48k = samples_48k[:SAMPLES_PER_FRAME_48K]

        frame = av.AudioFrame.from_ndarray(samples_48k.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = WEB_RTC_SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = self._time_base
        self._pts += SAMPLES_PER_FRAME_48K
        return frame

    def stop(self) -> None:
        self._running = False
        super().stop()


class ChatGPTRealtimePipeline:
    """Owns the real-time WebRTC Speech-to-Speech session for one phone call."""

    @staticmethod
    def _new_remote_audio_resampler() -> av.AudioResampler:
        """Create a stateful stereo/mono converter with one continuous filter."""

        return av.AudioResampler(format="s16", layout="mono", rate=PHONE_SAMPLE_RATE)

    def _default_language_code(self) -> str:
        return "fr" if self.config.providers.stt_language.lower().startswith("fr") else "en"

    def _build_session_instructions(self) -> str:
        default_language = "French" if self._default_language_code() == "fr" else "English"
        return (
            "### BILINGUAL CALL LANGUAGE ###\n"
            f"Begin in {default_language}. Understand both English and French. After the "
            "caller speaks a complete utterance, reply in the language they are currently "
            "using. Keep that language until they clearly switch or request another one. "
            "Do not switch because of a name, brand, isolated greeting, or borrowed word. "
            "Never answer in Spanish.\n\n"
            "### NATURAL TELEPHONE DELIVERY ###\n"
            "Speak at a relaxed, natural conversational pace with clear phrasing and brief "
            "human pauses. Use one or two short sentences and ask at most one question. Do "
            "not rush, stretch words, over-enunciate, use an announcer voice, or sound as if "
            "reading a script. Respond to attention checks and clarification requests before "
            "continuing the task.\n\n"
            f"{self.policy.system_prompt}"
        )

    def _build_session_update(self) -> dict[str, Any]:
        providers = self.config.providers
        languages = list(providers.chatgpt_realtime_input_languages)
        transcription: dict[str, Any] = {
            "model": providers.chatgpt_realtime_transcription_model,
            "prompt": (
                "Natural English or French cellular phone conversation. Conversation "
                "téléphonique naturelle en anglais ou en français. Domain vocabulary: "
                "OXzoon, IPTV Shopping, IPTV, live TV, télévision en direct, football, "
                "sports, smart TV, Firestick, streaming subscription, abonnement."
            ),
            "keywords": [
                "OXzoon",
                "IPTV Shopping",
                "IPTV",
                "live TV",
                "télévision en direct",
                "football",
                "Firestick",
                "streaming subscription",
                "abonnement",
            ],
        }
        if len(languages) == 1:
            transcription["language"] = languages[0]
        else:
            transcription["languages"] = languages

        noise_reduction: dict[str, str] | None = None
        if providers.chatgpt_realtime_noise_reduction != "off":
            noise_reduction = {"type": providers.chatgpt_realtime_noise_reduction}

        self._session_instructions = self._build_session_instructions()
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": self._session_instructions,
                "include": ["item.input_audio_transcription.logprobs"],
                "tools": tool_definitions(self.tool_catalog),
                "tool_choice": "auto",
                "audio": {
                    "input": {
                        # Cellular downlink has already been codec- and
                        # noise-processed. Additional near-field filtering can
                        # erase low-energy consonants before both VAD and model.
                        "noise_reduction": noise_reduction,
                        "transcription": transcription,
                        "turn_detection": {
                            "type": "semantic_vad",
                            "eagerness": providers.chatgpt_realtime_vad_eagerness,
                            "create_response": False,
                            # PhoneAgent admits completed meaningful turns and
                            # owns interruption/truncation atomically.
                            "interrupt_response": False,
                        },
                    },
                    "output": {"voice": self.voice},
                },
            },
        }

    def __init__(
        self,
        transport: PhoneAgentTransport,
        config: RuntimeConfig,
        *,
        auth_manager: ChatGPTAuthManager | None = None,
        caller_id: str = "anonymous",
        call_direction: str = "outbound",
        event_sink: EventSink | None = None,
        call_completion_sink: CallCompletionSink | None = None,
    ) -> None:
        self.transport = transport
        self.config = config
        self.auth_manager = auth_manager or ChatGPTAuthManager()
        self.caller_id = caller_id
        self.event_sink = event_sink
        self.call_completion_sink = call_completion_sink

        valid_chatgpt_voices = {
            "alloy",
            "ash",
            "ballad",
            "cedar",
            "coral",
            "echo",
            "sage",
            "shimmer",
            "verse",
            "marin",
        }
        requested_voice = (
            (config.providers.chatgpt_realtime_voice or config.providers.tts_voice_id or "")
            .strip()
            .lower()
        )
        if requested_voice in valid_chatgpt_voices:
            self.voice = requested_voice
        else:
            self.voice = "coral"

        requested_model = (config.providers.chatgpt_realtime_model or "auto").strip().lower()
        self.model = "gpt-realtime-2.1" if requested_model == "auto" else requested_model
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,80}", self.model):
            raise ValueError(f"Invalid Realtime model name: {self.model!r}")

        self.policy = AgentPolicyRuntime(
            caller_id=caller_id,
            task_id=config.task_id,
            language=config.providers.stt_language,
            call_direction=call_direction,
            additional_instructions=config.system_prompt,
            memory_enabled=config.memory_enabled,
            event_sink=event_sink,
        )
        self.tool_catalog = build_tool_catalog(self.policy.task_contract, self.policy.task)
        self.tool_catalog[END_CALL_TOOL_NAME] = build_end_call_tool()
        self.policy.task_contract["allowed_tools"] = sorted(
            {str(name) for name in self.policy.task_contract.get("allowed_tools", []) or []}
            | {END_CALL_TOOL_NAME}
        )
        identity_skill_tool = self.policy.persona_compiler.identity_kernel.realtime_skill_tool(
            task_id=self.policy.task_id,
            language=config.providers.stt_language,
            authorized_tools={
                str(name) for name in self.policy.task_contract.get("allowed_tools", []) or []
            },
        )
        if identity_skill_tool is not None:
            self.tool_catalog[identity_skill_tool.name] = identity_skill_tool
        self._contract_allowed_tools = {
            str(name) for name in self.policy.task_contract.get("allowed_tools", []) or []
        }
        self.mcp_broker = McpToolBroker.from_environment(
            task_allowed_tools=set(self._contract_allowed_tools),
            call_id=self.transport.session.call_id,
        )
        self.tool_control_store = ToolControlStore()
        self.managed_tool_runtime: ManagedToolRuntime | None = None
        self._retired_managed_tool_runtimes: list[ManagedToolRuntime] = []
        self._managed_tool_names: set[str] = set()
        self._tool_control_fingerprint = ""
        self._managed_tool_reload_lock = asyncio.Lock()
        self.openwa_config_store = OpenWAConfigStore()
        self.openwa_runtime: OpenWAToolRuntime | None = None
        self._retired_openwa_runtimes: list[OpenWAToolRuntime] = []
        self._openwa_tool_names: set[str] = set()
        self._openwa_fingerprint = ""
        self._openwa_reload_lock = asyncio.Lock()
        self.web_research_config_store = WebResearchConfigStore()
        self.web_research_runtime: WebResearchToolRuntime | None = None
        self._retired_web_research_runtimes: list[WebResearchToolRuntime] = []
        self._web_research_tool_names: set[str] = set()
        self._web_research_fingerprint = ""
        self._web_research_reload_lock = asyncio.Lock()
        self.frappe_config_store = FrappeConfigStore()
        self.frappe_runtime: FrappeToolRuntime | None = None
        self._retired_frappe_runtimes: list[FrappeToolRuntime] = []
        self._frappe_tool_names: set[str] = set()
        self._frappe_fingerprint = ""
        self._frappe_last_retry = 0.0
        self._frappe_reload_lock = asyncio.Lock()
        self.policy.available_tools = set(self.tool_catalog)

        self.device_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        self.voice_session_id = str(uuid.uuid4()).upper()

        self.pc: RTCPeerConnection | None = None
        self.dc: Any = None
        self.media_track: PhoneMediaStreamTrack | None = None
        self._started = asyncio.Event()
        self._session_updated = asyncio.Event()
        self._startup_error: RuntimeError | None = None
        self._running = False
        self._assistant_is_speaking = False
        self._drop_interrupted_remote_audio = False
        self._remote_audio_response_key: str | None = None
        self._remote_response_has_audible_audio = False
        self._remote_audio_resampler = self._new_remote_audio_resampler()
        self._remote_pcm_accumulator = bytearray()
        self._phone_audio_queue: asyncio.Queue[_PhoneAudioQueueItem] = asyncio.Queue(
            maxsize=REMOTE_AUDIO_QUEUE_FRAMES
        )
        self._remote_audio_frames = 0
        self._tasks: list[asyncio.Task[Any]] = []
        self._dc_messages: asyncio.Queue[str] = asyncio.Queue(maxsize=2048)
        self._current_assistant_text = ""
        self._responses: dict[str, _RealtimeResponseState] = {}
        self._active_response_key: str | None = None
        self._implicit_response_sequence = 0
        self._next_response_kind = "turn"
        self._response_phase = _ResponsePhase.IDLE
        self._creating_response_kind: str | None = None
        self._pending_response: tuple[dict[str, Any], str] | None = None
        self._cancel_on_create = False
        self._output_buffer_busy = False
        self._output_buffer_clear_pending = False
        self._processed_transcription_items: set[str] = set()
        self._awaiting_transcription_items: set[str] = set()
        self._speech_started_during_output: set[str] = set()
        self._confirmed_barge_in_items: set[str] = set()
        self._barge_in_tasks: dict[str, asyncio.Task[Any]] = {}
        self._caller_turn_response_pending = False
        self._transcription_failed_for_turn = False
        self._caller_turn_settle_task: asyncio.Task[Any] | None = None
        self._session_instructions = ""
        self._greeted = False
        self._greet_lock = asyncio.Lock()
        self._closed = False
        self._terminal_completion_notified = False
        self._ai_end_call_requested = False
        self._ai_end_call_reason: str | None = None
        self._refresh_tool_instructions()

    def _refresh_tool_instructions(self) -> None:
        self.policy.available_tools = set(self.tool_catalog)
        self.policy.system_prompt = self.policy.persona_compiler.compile(
            caller_memory=self.policy.caller_memory,
            task_contract=self.policy.task_contract,
            language=self.config.providers.stt_language,
            call_direction=self.policy.call_context.direction.value,
            additional_instructions=self.config.system_prompt,
            available_tools=self.policy.available_tools,
            caller_id=self.policy.caller_id,
        )
        self._session_instructions = self._build_session_instructions()

    def _refresh_dynamic_permissions(self) -> None:
        self.policy.task_contract["allowed_tools"] = sorted(
            self._contract_allowed_tools
            | self._managed_tool_names
            | self._openwa_tool_names
            | self._web_research_tool_names
            | self._frappe_tool_names
        )
        self._refresh_tool_instructions()

    async def _managed_tool_watcher(self) -> None:
        while self._running:
            await asyncio.sleep(1.0)
            try:
                fingerprint = await asyncio.to_thread(self.tool_control_store.fingerprint)
                if fingerprint != self._tool_control_fingerprint:
                    await self._reload_managed_tools(update_session=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("managed tool hot reload failed: %s", exc, exc_info=True)
                await self._emit({"type": "tools_reload_failed", "message": str(exc)[:500]})

    async def _openwa_watcher(self) -> None:
        while self._running:
            await asyncio.sleep(1.0)
            try:
                fingerprint = await asyncio.to_thread(self.openwa_config_store.fingerprint)
                if fingerprint != self._openwa_fingerprint:
                    await self._reload_openwa(update_session=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("OpenWA hot reload failed: %s", exc, exc_info=True)
                await self._emit({"type": "openwa_reload_failed", "message": str(exc)[:500]})

    async def _web_research_watcher(self) -> None:
        while self._running:
            await asyncio.sleep(1.0)
            try:
                fingerprint = await asyncio.to_thread(
                    self.web_research_config_store.fingerprint
                )
                if fingerprint != self._web_research_fingerprint:
                    await self._reload_web_research(update_session=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("web research hot reload failed: %s", exc, exc_info=True)
                await self._emit(
                    {"type": "web_research_reload_failed", "message": str(exc)[:500]}
                )

    async def _frappe_watcher(self) -> None:
        while self._running:
            await asyncio.sleep(1.0)
            try:
                config = await asyncio.to_thread(self.frappe_config_store.load)
                fingerprint = await asyncio.to_thread(self.frappe_config_store.fingerprint)
                retry_due = (
                    config.enabled
                    and
                    not self._frappe_tool_names
                    and time.monotonic() - self._frappe_last_retry >= 15.0
                )
                if fingerprint != self._frappe_fingerprint or retry_due:
                    self._frappe_last_retry = time.monotonic()
                    await self._reload_frappe(update_session=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Frappe hot reload failed: %s", exc, exc_info=True)
                await self._emit({"type": "frappe_reload_failed", "message": str(exc)[:500]})

    async def _reload_managed_tools(self, *, update_session: bool) -> None:
        async with self._managed_tool_reload_lock:
            config = await asyncio.to_thread(self.tool_control_store.load)
            fingerprint = await asyncio.to_thread(self.tool_control_store.fingerprint)
            candidate = ManagedToolRuntime(
                config,
                task_id=self.policy.task_id,
                call_id=str(self.transport.session.call_id),
                event_sink=self._emit,
            )
            try:
                managed = await candidate.start()
                retained = {
                    name: tool
                    for name, tool in self.tool_catalog.items()
                    if name not in self._managed_tool_names
                }
                collisions = set(retained) & set(managed)
                if collisions:
                    raise RuntimeError(
                        "managed tool name collides with an existing tool: "
                        + ", ".join(sorted(collisions))
                    )
            except Exception:
                await candidate.close()
                raise
            previous = self.managed_tool_runtime
            self.managed_tool_runtime = candidate
            self._managed_tool_names = set(managed)
            self.tool_catalog = {**retained, **managed}
            self._refresh_dynamic_permissions()
            self._tool_control_fingerprint = fingerprint
            if previous is not None:
                self._retired_managed_tool_runtimes.append(previous)
            if update_session and self.dc and self.dc.readyState == "open":
                self.send_event(
                    {
                        "type": "session.update",
                        "event_id": self._client_event_id("managed_tools"),
                        "session": {
                            "type": "realtime",
                            "instructions": self._session_instructions,
                            "tools": tool_definitions(self.tool_catalog),
                            "tool_choice": "auto",
                        },
                    }
                )
            await self._emit(
                {
                    "type": "tools_reloaded",
                    "revision": config.revision,
                    "active_tools": sorted(self._managed_tool_names),
                    "live": bool(update_session),
                }
            )

    async def _reload_openwa(self, *, update_session: bool) -> None:
        async with self._openwa_reload_lock:
            config = await asyncio.to_thread(self.openwa_config_store.load)
            fingerprint = await asyncio.to_thread(self.openwa_config_store.fingerprint)
            candidate = OpenWAToolRuntime(
                config,
                caller_id=self.caller_id,
                task_id=self.policy.task_id,
                call_id=str(self.transport.session.call_id),
                event_sink=self._emit,
                conversation_sink=self._inject_openwa_context,
            )
            try:
                tools = await candidate.start()
                retained = {
                    name: tool
                    for name, tool in self.tool_catalog.items()
                    if name not in self._openwa_tool_names
                }
                collisions = set(retained) & set(tools)
                if collisions:
                    raise RuntimeError(
                        "OpenWA tool name collides with an existing tool: "
                        + ", ".join(sorted(collisions))
                    )
            except Exception:
                await candidate.close()
                raise
            previous = self.openwa_runtime
            self.openwa_runtime = candidate
            self._openwa_tool_names = set(tools)
            self.tool_catalog = {**retained, **tools}
            self._refresh_dynamic_permissions()
            self._openwa_fingerprint = fingerprint
            if previous is not None:
                await previous.retire()
                self._retired_openwa_runtimes.append(previous)
            if update_session and self.dc and self.dc.readyState == "open":
                self.send_event(
                    {
                        "type": "session.update",
                        "event_id": self._client_event_id("openwa_tools"),
                        "session": {
                            "type": "realtime",
                            "instructions": self._session_instructions,
                            "tools": tool_definitions(self.tool_catalog),
                            "tool_choice": "auto",
                        },
                    }
                )
            await self._emit(
                {
                    "type": "openwa_tools_reloaded",
                    "revision": config.revision,
                    "active_tools": sorted(self._openwa_tool_names),
                    "live": bool(update_session),
                }
            )

    async def _reload_web_research(self, *, update_session: bool) -> None:
        async with self._web_research_reload_lock:
            config = await asyncio.to_thread(self.web_research_config_store.load)
            fingerprint = await asyncio.to_thread(
                self.web_research_config_store.fingerprint
            )
            candidate = WebResearchToolRuntime(
                config,
                task_id=self.policy.task_id,
                event_sink=self._emit,
            )
            try:
                tools = await candidate.start()
                retained = {
                    name: tool
                    for name, tool in self.tool_catalog.items()
                    if name not in self._web_research_tool_names
                }
                collisions = set(retained) & set(tools)
                if collisions:
                    raise RuntimeError(
                        "web research tool name collides with an existing tool: "
                        + ", ".join(sorted(collisions))
                    )
            except Exception:
                await candidate.close()
                raise
            previous = self.web_research_runtime
            self.web_research_runtime = candidate
            self._web_research_tool_names = set(tools)
            self.tool_catalog = {**retained, **tools}
            self._refresh_dynamic_permissions()
            self._web_research_fingerprint = fingerprint
            if previous is not None:
                self._retired_web_research_runtimes.append(previous)
            if update_session and self.dc and self.dc.readyState == "open":
                self.send_event(
                    {
                        "type": "session.update",
                        "event_id": self._client_event_id("web_research_tools"),
                        "session": {
                            "type": "realtime",
                            "instructions": self._session_instructions,
                            "tools": tool_definitions(self.tool_catalog),
                            "tool_choice": "auto",
                        },
                    }
                )
            await self._emit(
                {
                    "type": "web_research_tools_reloaded",
                    "revision": config.revision,
                    "active_tools": sorted(self._web_research_tool_names),
                    "live": bool(update_session),
                }
            )

    async def _reload_frappe(self, *, update_session: bool) -> None:
        async with self._frappe_reload_lock:
            config = await asyncio.to_thread(self.frappe_config_store.load)
            fingerprint = await asyncio.to_thread(self.frappe_config_store.fingerprint)
            candidate = FrappeToolRuntime(
                config,
                caller_id=self.caller_id,
                task_id=self.policy.task_id,
                call_id=str(self.transport.session.call_id),
                call_direction=self.policy.call_context.direction.value,
                event_sink=self._emit,
            )
            try:
                tools = await candidate.start()
                retained = {
                    name: tool
                    for name, tool in self.tool_catalog.items()
                    if name not in self._frappe_tool_names
                }
                collisions = set(retained) & set(tools)
                if collisions:
                    raise RuntimeError(
                        "Frappe tool name collides with an existing tool: "
                        + ", ".join(sorted(collisions))
                    )
            except Exception:
                await candidate.close()
                raise
            previous = self.frappe_runtime
            self.frappe_runtime = candidate
            self._frappe_tool_names = set(tools)
            self.tool_catalog = {**retained, **tools}
            self._refresh_dynamic_permissions()
            self._frappe_fingerprint = fingerprint
            if previous is not None:
                self._retired_frappe_runtimes.append(previous)
            if update_session and self.dc and self.dc.readyState == "open":
                self.send_event(
                    {
                        "type": "session.update",
                        "event_id": self._client_event_id("frappe_tools"),
                        "session": {
                            "type": "realtime",
                            "instructions": self._session_instructions,
                            "tools": tool_definitions(self.tool_catalog),
                            "tool_choice": "auto",
                        },
                    }
                )
            await self._emit(
                {
                    "type": "frappe_tools_reloaded",
                    "revision": config.revision,
                    "active_tools": sorted(self._frappe_tool_names),
                    "live": bool(update_session),
                }
            )

    async def _inject_openwa_context(self, text: str, respond: bool) -> None:
        self.add_external_context(text, respond=respond)

    async def start(self, timeout_secs: float = 20.0) -> None:
        """Establish direct Developer WebRTC connection with OpenAI Realtime Gateway."""
        if self._running:
            return
        self._running = True
        mcp_tools = await self.mcp_broker.start()
        collisions = set(self.tool_catalog) & set(mcp_tools)
        if collisions:
            raise RuntimeError("MCP tool name collision after namespace mapping")
        self.tool_catalog.update(mcp_tools)
        await self._reload_managed_tools(update_session=False)
        await self._reload_openwa(update_session=False)
        await self._reload_web_research(update_session=False)
        logger.info(
            "connecting Developer Realtime S2S WebRTC pipeline call_id=%s model=%s voice=%s",
            self.transport.session.call_id,
            self.model,
            self.voice,
        )
        self._tasks.append(
            asyncio.create_task(
                self._send_phone_audio_queue(),
                name="chatgpt-phone-audio-sender",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self._managed_tool_watcher(),
                name="chatgpt-managed-tool-watcher",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self._openwa_watcher(),
                name="chatgpt-openwa-watcher",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self._web_research_watcher(),
                name="chatgpt-web-research-watcher",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self._frappe_watcher(),
                name="chatgpt-frappe-watcher",
            )
        )

        try:
            token = await asyncio.to_thread(self.auth_manager.get_token)
        except Exception as exc:
            logger.error("Could not obtain auth token for Realtime S2S: %s", exc)
            raise

        # 1. Setup local WebRTC PeerConnection
        self.pc = RTCPeerConnection()

        # 2. Setup Phone Media Stream Track for Downlink (Phone Mic -> OpenAI)
        self.media_track = PhoneMediaStreamTrack()
        self.pc.addTrack(self.media_track)

        # Hook transport input so incoming phone downlink frames reach the WebRTC track
        self.transport.add_audio_listener(self.media_track.push_pcm_frame)

        # 3. Handle incoming remote audio track from OpenAI (OpenAI -> Phone Earpiece)
        @self.pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            if track.kind == "audio":
                logger.info(
                    "OpenAI Realtime WebRTC audio stream connected call_id=%s",
                    self.transport.session.call_id,
                )
                task = asyncio.create_task(
                    self._pump_remote_audio_to_phone(track),
                    name="chatgpt-webrtc-uplink-pump",
                )
                self._tasks.append(task)

        # 4. Setup DataChannel for bidirectional Realtime control events
        self.dc = self.pc.createDataChannel("oai-events")
        self._tasks.append(
            asyncio.create_task(
                self._consume_dc_messages(),
                name="chatgpt-realtime-event-consumer",
            )
        )

        @self.dc.on("open")
        def on_dc_open() -> None:
            logger.info(
                "OpenAI Realtime DataChannel open call_id=%s id=%s",
                self.transport.session.call_id,
                self.dc.id if self.dc else None,
            )
            # Bind exact compiled persona and task contract into the Realtime session.
            self.send_event(self._build_session_update())

        @self.dc.on("message")
        def on_dc_message(message: str) -> None:
            try:
                self._dc_messages.put_nowait(str(message))
            except asyncio.QueueFull:
                self._startup_error = RuntimeError("Realtime event queue overflow")
                self._started.set()

        # 5. Generate SDP Offer
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        sdp_offer = self.pc.localDescription.sdp if self.pc.localDescription else ""

        # 6. Direct Developer Realtime GA Gateway Handshake
        url = f"https://api.openai.com/v1/realtime/calls?model={quote(self.model, safe='-._')}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/sdp",
        }

        async with AsyncSession(impersonate="safari17_0") as session:
            res = await session.post(url, headers=headers, data=sdp_offer, timeout=15)
            if res.status_code == 401:
                token = await asyncio.to_thread(self.auth_manager.get_token, force_refresh=True)
                headers["Authorization"] = f"Bearer {token}"
                res = await session.post(url, headers=headers, data=sdp_offer, timeout=15)

            if res.status_code not in (200, 201):
                msg = res.text[:200]
                raise RuntimeError(
                    f"OpenAI Developer Realtime signaling failed ({res.status_code}): {msg}"
                )

            answer_sdp = res.text

        # 7. Set remote SDP answer
        answer = RTCSessionDescription(sdp=answer_sdp, type="answer")
        await self.pc.setRemoteDescription(answer)

        # 8. Wait for session.updated confirmation
        try:
            await asyncio.wait_for(self._started.wait(), timeout=timeout_secs)
            if self._startup_error is not None:
                raise self._startup_error
            logger.info(
                "OpenAI Developer Realtime S2S pipeline ready and confirmed call_id=%s",
                self.transport.session.call_id,
            )
        except Exception as exc:
            await self.cancel(f"DataChannel startup failed: {exc}")
            raise

    async def _consume_dc_messages(self) -> None:
        """Process Realtime events in wire order, including transcript/state updates."""
        while self._running:
            message = await self._dc_messages.get()
            try:
                await self._handle_dc_message(message)
            finally:
                self._dc_messages.task_done()

    async def _pump_remote_audio_to_phone(self, track: MediaStreamTrack) -> None:
        """Receive and convert RTP without blocking on the slower cellular playout clock."""

        first_frame = True

        try:
            while self._running:
                frame = await track.recv()
                if first_frame:
                    first_frame = False
                    logger.info(
                        "Received first remote OpenAI audio frame from WebRTC track call_id=%s",
                        self.transport.session.call_id,
                    )
                if self._drop_interrupted_remote_audio or self._remote_audio_response_key is None:
                    continue
                response_key = self._remote_audio_response_key
                state = self._responses.get(response_key)
                if state is None or state.interrupted:
                    continue

                converted_frames = self._remote_audio_resampler.resample(frame)
                for converted in converted_frames:
                    mono = converted.to_ndarray().reshape(-1).astype(np.int16, copy=False)
                    rms = (
                        float(np.sqrt(np.mean(mono.astype(np.float64) ** 2))) if mono.size else 0.0
                    )

                    # OpenAI's receiver is live before and after speech. Drop
                    # leading idle packets, then preserve pauses inside speech.
                    if not self._remote_response_has_audible_audio:
                        if rms < 20.0:
                            continue
                        self._remote_response_has_audible_audio = True

                    self._remote_pcm_accumulator.extend(mono.tobytes())

                    while len(self._remote_pcm_accumulator) >= PHONE_CHUNK_BYTES:
                        chunk = bytes(self._remote_pcm_accumulator[:PHONE_CHUNK_BYTES])
                        del self._remote_pcm_accumulator[:PHONE_CHUNK_BYTES]
                        self._remote_audio_frames += 1
                        await self._phone_audio_queue.put(
                            _PhoneAudioQueueItem(response_key=response_key, pcm=chunk)
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._running:
                logger.warning("WebRTC audio receiver ended: %s", exc)

    async def _send_phone_audio_queue(self) -> None:
        """Serialize converted response audio onto Android's acknowledged playout clock."""

        while self._running:
            item = await self._phone_audio_queue.get()
            try:
                state = self._responses.get(item.response_key)
                if state is None or state.interrupted or state.playback_closed:
                    continue
                if item.pcm is None:
                    await self._finish_phone_audio_item(state)
                    continue

                self._assistant_is_speaking = True
                result = await self.transport.output().write_audio_frame_result(
                    OutputAudioRawFrame(
                        audio=item.pcm,
                        sample_rate=PHONE_SAMPLE_RATE,
                        num_channels=1,
                    )
                )
                if result is AudioWriteResult.DELIVERED:
                    continue
                if result is AudioWriteResult.CANCELLED:
                    logger.info(
                        "Realtime phone audio cancelled normally response=%s",
                        item.response_key,
                    )
                    continue

                message = "Generated Realtime audio could not reach Android telephony playout"
                await self._mark_response_audio_failed(state, message)
                await self._emit({"type": "call_error", "message": message})
                state.playback_closed = True
                if state.kind == "terminal":
                    await self._notify_terminal_completion(
                        f"AI ended call after closing audio failure: {message}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state = self._responses.get(item.response_key)
                if state is not None and not state.playback_closed:
                    await self._mark_response_audio_failed(state, str(exc))
                    await self._emit({"type": "call_error", "message": str(exc)})
                    state.playback_closed = True
                    if state.kind == "terminal":
                        await self._notify_terminal_completion(
                            f"AI ended call after closing audio failure: {exc}"
                        )
            finally:
                self._phone_audio_queue.task_done()

    async def _finish_phone_audio_item(self, state: _RealtimeResponseState) -> None:
        state.audio_end = await self.transport.output().finish_audio_segment()
        if state.audio_end is None:
            message = "Realtime response ended without phone-deliverable audio"
            await self._mark_response_audio_failed(state, message)
            await self._emit({"type": "call_error", "message": message})
            state.playback_closed = True
            if state.kind == "terminal":
                await self._notify_terminal_completion(
                    f"AI ended call after closing audio failure: {message}"
                )
            return
        self._start_playback_monitor(state)

    async def _mark_response_audio_failed(
        self, state: _RealtimeResponseState, message: str
    ) -> None:
        """Attach a transport failure to the response even if transcript finalization lags."""

        if not state.finalized.is_set():
            try:
                await asyncio.wait_for(state.finalized.wait(), timeout=3.0)
            except TimeoutError:
                pass
        await self.policy.playback_failed(message)

    def _response_key(self, ev: dict[str, Any], payload: dict[str, Any]) -> str | None:
        for source in (ev, payload):
            response = source.get("response")
            if isinstance(response, dict) and response.get("id"):
                return str(response["id"])
        for source in (ev, payload):
            if source.get("response_id"):
                return str(source["response_id"])
        return self._active_response_key

    def _response_state(
        self,
        ev: dict[str, Any],
        payload: dict[str, Any],
        *,
        kind: str | None = None,
    ) -> _RealtimeResponseState:
        key = self._response_key(ev, payload)
        if key is None:
            self._implicit_response_sequence += 1
            key = f"implicit-{self._implicit_response_sequence}"
        state = self._responses.get(key)
        if state is None:
            state = _RealtimeResponseState(
                key=key,
                kind=kind or self._creating_response_kind or self._next_response_kind,
                rendered_at_start=self.transport.session.metrics.last_rendered_sequence,
                remote_audio_frames_at_start=self._remote_audio_frames,
            )
            self._responses[key] = state
            self._next_response_kind = "turn"
        return state

    @staticmethod
    def _client_event_id(prefix: str) -> str:
        return f"phoneagent_{prefix}_{uuid.uuid4().hex}"

    def _dispatch_response(self, event: dict[str, Any], kind: str) -> None:
        """Send exactly one response.create while transitioning IDLE -> CREATING."""

        if self._response_phase is not _ResponsePhase.IDLE:
            self._pending_response = (event, kind)
            logger.info(
                "Coalesced Realtime response request phase=%s active_response=%s kind=%s",
                self._response_phase,
                self._active_response_key,
                kind,
            )
            return

        outbound = dict(event)
        event_id = self._client_event_id("response_create")
        outbound["event_id"] = event_id
        self._response_phase = _ResponsePhase.CREATING
        self._creating_response_kind = kind
        self._next_response_kind = kind
        self.send_event(outbound)
        logger.info("Sent Realtime response.create event_id=%s kind=%s", event_id, kind)

    def _request_response(self, event: dict[str, Any], kind: str = "turn") -> None:
        """Serialize response generation; the newest queued caller turn wins."""

        if (
            self._response_phase is _ResponsePhase.IDLE
            and not self._output_buffer_clear_pending
            and not self._output_buffer_busy
        ):
            self._dispatch_response(event, kind)
        else:
            self._pending_response = (event, kind)
            logger.info(
                "Queued Realtime response until response.done phase=%s active_response=%s",
                self._response_phase,
                self._active_response_key,
            )

    def _cancel_active_response(self) -> None:
        """Cancel the known active response and wait for response.done before reuse."""

        if (
            self._response_phase is not _ResponsePhase.IN_PROGRESS
            or self._active_response_key is None
        ):
            return
        event_id = self._client_event_id("response_cancel")
        response_id = self._active_response_key
        self._response_phase = _ResponsePhase.CANCELLING
        self.send_event(
            {
                "type": "response.cancel",
                "event_id": event_id,
                "response_id": response_id,
            }
        )
        logger.info(
            "Sent Realtime response.cancel event_id=%s response_id=%s",
            event_id,
            response_id,
        )

    def _clear_server_output_buffer(self) -> None:
        """Discard paced WebRTC audio so an interrupted tail cannot leak into the next turn."""

        if self._output_buffer_clear_pending:
            return
        event_id = self._client_event_id("output_clear")
        self._output_buffer_clear_pending = True
        self.send_event({"type": "output_audio_buffer.clear", "event_id": event_id})
        logger.info("Sent Realtime output_audio_buffer.clear event_id=%s", event_id)

    def _dispatch_pending_if_ready(self) -> None:
        if (
            self._response_phase is not _ResponsePhase.IDLE
            or self._output_buffer_clear_pending
            or self._output_buffer_busy
            or self._pending_response is None
        ):
            return
        pending = self._pending_response
        self._pending_response = None
        self._dispatch_response(*pending)

    def _complete_response_lifecycle(self, ev: dict[str, Any], payload: dict[str, Any]) -> None:
        response_key = self._response_key(ev, payload)
        response = ev.get("response") or payload.get("response") or {}
        status = str(response.get("status", "unknown")) if isinstance(response, dict) else "unknown"
        logger.info(
            "Realtime response.done response_id=%s status=%s phase=%s",
            response_key,
            status,
            self._response_phase,
        )
        if response_key and self._active_response_key not in {None, response_key}:
            logger.warning(
                "Ignored stale response.done response_id=%s active_response=%s",
                response_key,
                self._active_response_key,
            )
            return
        self._active_response_key = None
        self._response_phase = _ResponsePhase.IDLE
        self._creating_response_kind = None
        self._cancel_on_create = False
        self._dispatch_pending_if_ready()

    @staticmethod
    def _transcription_item_id(ev: dict[str, Any], payload: dict[str, Any]) -> str | None:
        for source in (ev, payload):
            if source.get("item_id"):
                return str(source["item_id"])
            item = source.get("item")
            if isinstance(item, dict) and item.get("id"):
                return str(item["id"])
        return None

    @staticmethod
    def _transcription_metadata(
        ev: dict[str, Any], payload: dict[str, Any]
    ) -> tuple[str | None, float | None]:
        """Extract detected language and geometric token confidence when supplied."""

        language: str | None = None
        logprobs: list[dict[str, Any]] = []
        for source in (ev, payload):
            languages = source.get("languages")
            if language is None and isinstance(languages, list):
                for candidate in languages:
                    code = candidate.get("code") if isinstance(candidate, dict) else candidate
                    normalized = str(code or "").strip().lower()
                    if normalized:
                        language = normalized.split("-", 1)[0]
                        break
            candidate_logprobs = source.get("logprobs")
            if isinstance(candidate_logprobs, list):
                logprobs = [item for item in candidate_logprobs if isinstance(item, dict)]
                if logprobs:
                    break

        values: list[float] = []
        for item in logprobs:
            try:
                value = float(item["logprob"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(value):
                values.append(value)
        confidence = None
        if values:
            mean_logprob = sum(values) / len(values)
            confidence = math.exp(min(0.0, max(-20.0, mean_logprob)))
        return language, confidence

    def _assistant_output_active(self) -> bool:
        state = self._responses.get(
            self._active_response_key or self._remote_audio_response_key or ""
        )
        return (
            self._response_phase is not _ResponsePhase.IDLE
            or self._output_buffer_busy
            or self._assistant_is_speaking
            or (state is not None and not state.playback_closed)
        )

    def _capture_output_item_identity(
        self,
        state: _RealtimeResponseState,
        ev: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        item_id = self._transcription_item_id(ev, payload)
        item = ev.get("item") or payload.get("item") or {}
        if isinstance(item, dict) and item.get("id"):
            item_id = str(item["id"])
        if item_id:
            state.output_item_id = item_id
        raw_index = ev.get("content_index", payload.get("content_index"))
        if isinstance(raw_index, int) and raw_index >= 0:
            state.output_content_index = raw_index

    def _truncate_interrupted_output(self, state: _RealtimeResponseState | None) -> None:
        if state is None or not state.output_item_id:
            logger.info("Realtime interruption had no output item available to truncate")
            return
        delivered_frames = max(
            0,
            self.transport.session.metrics.last_rendered_sequence - state.rendered_at_start,
        )
        audio_end_ms = int(delivered_frames * FRAME_DURATION * 1000)
        event_id = self._client_event_id("item_truncate")
        self.send_event(
            {
                "type": "conversation.item.truncate",
                "event_id": event_id,
                "item_id": state.output_item_id,
                "content_index": state.output_content_index,
                "audio_end_ms": audio_end_ms,
            }
        )
        logger.info(
            "Truncated interrupted Realtime output item_id=%s audio_end_ms=%d event_id=%s",
            state.output_item_id,
            audio_end_ms,
            event_id,
        )

    async def _interrupt_assistant_for_caller(self, item_id: str, *, reason: str) -> bool:
        """Atomically stop generated, server-buffered, and phone-buffered speech."""

        if item_id in self._confirmed_barge_in_items:
            return True
        state = self._responses.get(
            self._active_response_key or self._remote_audio_response_key or ""
        )
        if not self._assistant_output_active():
            return False
        self._confirmed_barge_in_items.add(item_id)
        if state is not None:
            state.interrupted = True

        if self._response_phase is _ResponsePhase.CREATING:
            self._cancel_on_create = True
        elif self._response_phase is _ResponsePhase.IN_PROGRESS:
            self._cancel_active_response()

        server_output_active = (
            self._response_phase
            in {
                _ResponsePhase.IN_PROGRESS,
                _ResponsePhase.CANCELLING,
            }
            or self._output_buffer_busy
        )
        if server_output_active:
            self._clear_server_output_buffer()
        self._truncate_interrupted_output(state)

        self._drop_interrupted_remote_audio = True
        self._remote_audio_response_key = None
        self._remote_response_has_audible_audio = False
        self._remote_pcm_accumulator.clear()
        self._assistant_is_speaking = False
        await self.transport.coordinator.interrupt(
            reason,
            self.transport.output()._flush_phone,
        )
        self.transport.output().discard_audio_segment()
        logger.info("Confirmed caller barge-in item_id=%s reason=%s", item_id, reason)
        return True

    def _schedule_barge_in_confirmation(self, item_id: str) -> None:
        previous = self._barge_in_tasks.pop(item_id, None)
        if previous is not None and not previous.done():
            previous.cancel()

        async def confirm() -> None:
            await asyncio.sleep(BARGE_IN_CONFIRM_SECS)
            if not self._assistant_output_active():
                return
            if self.media_track is not None and not self.media_track.speech_active:
                logger.info(
                    "Ignored brief caller speech during assistant output item_id=%s",
                    item_id,
                )
                return
            await self._interrupt_assistant_for_caller(
                item_id,
                reason="chatgpt_sustained_caller_speech",
            )

        task = asyncio.create_task(confirm(), name=f"barge-in-confirm-{item_id}")
        self._barge_in_tasks[item_id] = task
        self._tasks.append(task)

        def forget(completed: asyncio.Task[Any]) -> None:
            if self._barge_in_tasks.get(item_id) is completed:
                self._barge_in_tasks.pop(item_id, None)

        task.add_done_callback(forget)

    def _cancel_caller_turn_settle(self) -> None:
        task = self._caller_turn_settle_task
        if task is not None and not task.done():
            task.cancel()
        self._caller_turn_settle_task = None

    def _schedule_caller_turn_response(self) -> None:
        self._cancel_caller_turn_settle()

        async def settle() -> None:
            await asyncio.sleep(CALLER_TURN_SETTLE_SECS)
            if self._ai_end_call_requested:
                return
            if self._awaiting_transcription_items or not self._caller_turn_response_pending:
                return
            self._caller_turn_response_pending = False
            self._request_response(self._next_turn_response_event())

        task = asyncio.create_task(settle(), name="realtime-caller-turn-settle")
        self._caller_turn_settle_task = task
        self._tasks.append(task)

        def forget(completed: asyncio.Task[Any]) -> None:
            if self._caller_turn_settle_task is completed:
                self._caller_turn_settle_task = None

        task.add_done_callback(forget)

    def _transcript_needs_response(
        self,
        transcript: str,
        quality: TurnQuality,
        *,
        started_during_output: bool,
    ) -> bool:
        if quality in {TurnQuality.BACKCHANNEL, TurnQuality.UNINTELLIGIBLE}:
            return False
        expected = self.policy.matches_expected_answer(transcript)
        explicit_control = self.policy.is_explicit_conversation_control(transcript)
        tokens = words_of(normalize(transcript))
        if quality is TurnQuality.FRAGMENT:
            return explicit_control and not started_during_output
        if len(tokens) == 1 and not (expected or explicit_control):
            if started_during_output or self._caller_turn_response_pending:
                return False
            if normalize(transcript) in {"you", "peace"}:
                return False
        if started_during_output and len(tokens) <= 2:
            return expected or explicit_control
        return True

    async def _record_ignored_transcription(
        self, transcript: str, *, item_id: str | None, quality: TurnQuality
    ) -> None:
        logger.info(
            "Suppressed non-actionable caller fragment item_id=%s quality=%s text=%s",
            item_id,
            quality,
            transcript,
        )
        await self._emit(
            {
                "type": "transcript",
                "role": "user",
                "text": transcript,
                "turn_admission": "ignored",
            }
        )

    def _next_turn_response_event(self) -> dict[str, Any]:
        """Create one tightly scoped turn instruction without a policy round-trip."""

        state = self.policy.live_state_instructions()
        if self.policy._last_caller_intent in {"goodbye", "permission_refused"}:
            action = (
                "Decide whether the live conversation is now genuinely finished. If it is, "
                "call end_call with one brief, polite goodbye in the caller's language; do not "
                "speak that closing separately. Do not ask a question, continue selling, or "
                "restart the opening."
            )
        elif self.policy._last_caller_intent == "attention_check":
            action = (
                "Confirm naturally and briefly that you are still there and can hear the caller. "
                "Ask only whether they can hear you clearly. Do not repeat the introduction, "
                "sales pitch, permission question, or callback question."
            )
        elif (
            self.policy._last_caller_intent == "uncertain_audio"
            or self._transcription_failed_for_turn
        ):
            action = (
                "The separate side transcription was missing or low-confidence. Listen to "
                "the caller's original audio in the conversation. If their meaning is clear "
                "to you, answer that exact meaning directly. Otherwise ask them once, briefly "
                "and naturally, to repeat. Do not guess, fill a task field, advance the sales "
                "script, repeat the opening, or ask the permission question again."
            )
        elif (
            self.policy._permission_state == "unknown"
            and self.policy.last_caller_text.rstrip().endswith("?")
        ):
            action = (
                "The caller asked something that may have been transcribed unclearly. Address "
                "their exact words first. If their meaning is unclear, apologize once and ask "
                "them to repeat it. Do not reinterpret it as a bad time, offer a callback, or "
                "repeat the opening's permission question."
            )
        else:
            action = (
                "Respond naturally to the caller's latest meaning and execute the next useful "
                "step in the active task. Stay in persona, do not repeat anything already asked, "
                "and ask at most one short question."
            )
        event = {
            "type": "response.create",
            "response": {"instructions": f"{action}\n\n{state}"},
        }
        self._transcription_failed_for_turn = False
        return event

    def _schedule_audio_finish(self, state: _RealtimeResponseState) -> None:
        if state.audio_finish_started or state.playback_closed:
            return
        state.audio_finish_started = True
        task = asyncio.create_task(
            self._finish_audio_after_media_settles(state),
            name=f"realtime-audio-finish-{state.key}",
        )
        self._tasks.append(task)

    async def _finish_audio_after_media_settles(self, state: _RealtimeResponseState) -> None:
        """Put the phone marker behind OpenAI's authoritative playback boundary."""

        try:
            if state.interrupted:
                self.transport.output().discard_audio_segment()
                return
            # response.output_audio.done means generation is complete; it does
            # not mean the WebRTC output buffer has played every packet. The
            # server's output_audio_buffer.stopped event is the normal terminal
            # signal. A clear wakes this waiter too and is handled as an
            # interruption. The timeout is only a malformed-peer fallback.
            timed_out = False
            try:
                await asyncio.wait_for(
                    state.output_buffer_terminal.wait(),
                    timeout=REMOTE_AUDIO_DRAIN_TIMEOUT_SECS,
                )
            except TimeoutError:
                timed_out = True
                logger.warning(
                    "Timed out waiting for Realtime output buffer terminal event "
                    "response=%s; closing at latest RTP frame",
                    state.key,
                )
            if state.interrupted or (
                state.output_buffer_terminal.is_set() and not state.output_buffer_stopped
            ):
                self.transport.output().discard_audio_segment()
                return
            if not timed_out:
                await asyncio.sleep(RTP_SETTLE_AFTER_OUTPUT_STOP_SECS)
                if state.interrupted:
                    self.transport.output().discard_audio_segment()
                    return
            if self._remote_audio_response_key == state.key:
                self._remote_audio_response_key = None
                self._remote_response_has_audible_audio = False
            # Stateful conversion retains a few filter-tail samples. Flush them
            # once, after RTP drain, so the final phoneme is not clipped.
            for converted in self._remote_audio_resampler.resample(None):
                tail = converted.to_ndarray().reshape(-1).astype(np.int16, copy=False)
                self._remote_pcm_accumulator.extend(tail.tobytes())
            if self._remote_pcm_accumulator:
                remainder = bytes(self._remote_pcm_accumulator)
                self._remote_pcm_accumulator.clear()
                remainder += b"\x00" * (PHONE_CHUNK_BYTES - len(remainder))
                self._remote_audio_frames += 1
                await self._phone_audio_queue.put(
                    _PhoneAudioQueueItem(response_key=state.key, pcm=remainder)
                )
            # The sender consumes this marker only after every preceding chunk
            # for the response has crossed the authenticated phone link.
            await self._phone_audio_queue.put(_PhoneAudioQueueItem(response_key=state.key))
            logger.info(
                "Realtime phone audio finalized after output buffer stopped "
                "response=%s phone_frames=%d",
                state.key,
                self._remote_audio_frames - state.remote_audio_frames_at_start,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not state.playback_closed:
                await self.policy.playback_failed(str(exc))
                await self._emit({"type": "call_error", "message": str(exc)})
                state.playback_closed = True
                if state.kind == "terminal":
                    await self._notify_terminal_completion(
                        f"AI ended call after closing audio failure: {exc}"
                    )

    async def _finalize_response_state(self, state: _RealtimeResponseState) -> None:
        if state.finalized.is_set():
            return
        completed_text = state.text.strip()
        if completed_text:
            await self.policy.finalize_response(completed_text, response_kind=state.kind)
            await self.policy.playback_started()
            if state.interrupted:
                await self.policy.mark_playback_interrupted()
                delivered = max(
                    0,
                    self.transport.session.metrics.last_rendered_sequence - state.rendered_at_start,
                )
                await self.policy.playback_stopped(delivered_frames=delivered)
                state.playback_closed = True
        state.finalized.set()
        if state.audio_end is not None and not state.playback_closed:
            self._start_playback_monitor(state)
        self._current_assistant_text = ""

    def _start_playback_monitor(self, state: _RealtimeResponseState) -> None:
        if state.monitor_started or state.playback_closed or state.audio_end is None:
            return
        state.monitor_started = True
        task = asyncio.create_task(
            self._monitor_phone_playback(state),
            name=f"phone-playback-{state.key}",
        )
        self._tasks.append(task)

    async def _monitor_phone_playback(self, state: _RealtimeResponseState) -> None:
        try:
            await asyncio.wait_for(state.finalized.wait(), timeout=3.0)
            if state.playback_closed or state.audio_end is None:
                return
            generation_id, end_sequence = state.audio_end
            deadline = asyncio.get_running_loop().time() + 6.0
            while self._running and asyncio.get_running_loop().time() < deadline:
                if self.transport.session.generation_id != generation_id:
                    await self.policy.mark_playback_interrupted()
                    await self.policy.playback_stopped(
                        delivered_frames=max(
                            0,
                            self.transport.session.metrics.last_rendered_sequence
                            - state.rendered_at_start,
                        )
                    )
                    state.playback_closed = True
                    self._assistant_is_speaking = False
                    if state.kind == "terminal":
                        await self._notify_terminal_completion(
                            "AI ended call after closing playout interruption"
                        )
                    return
                if self.transport.session.metrics.last_rendered_sequence >= end_sequence:
                    await self.policy.playback_stopped(
                        delivered_frames=max(
                            1,
                            self.transport.session.metrics.last_rendered_sequence
                            - state.rendered_at_start,
                        )
                    )
                    state.playback_closed = True
                    self._assistant_is_speaking = False
                    if state.kind == "terminal":
                        reason = self._ai_end_call_reason or "conversation complete"
                        await self._notify_terminal_completion(f"AI ended call: {reason}")
                    return
                await asyncio.sleep(0.01)
            if not state.playback_closed:
                message = "Android did not acknowledge rendering this Realtime response"
                await self.policy.playback_failed(message)
                await self._emit({"type": "call_error", "message": message})
                state.playback_closed = True
                self._assistant_is_speaking = False
                if state.kind == "terminal":
                    await self._notify_terminal_completion(
                        f"AI ended call after closing audio failure: {message}"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not state.playback_closed:
                await self.policy.playback_failed(str(exc))
                state.playback_closed = True
                if state.kind == "terminal":
                    await self._notify_terminal_completion(
                        f"AI ended call after closing audio failure: {exc}"
                    )

    async def _handle_dc_message(self, message: str) -> None:
        """Process one ordered Realtime DataChannel event."""
        try:
            ev = json.loads(message)
            if ev.get("type") == "data_message" and isinstance(ev.get("data"), str):
                try:
                    inner = json.loads(ev["data"])
                    if isinstance(inner, dict):
                        ev = inner
                except Exception:
                    pass

            payload = ev.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}
            event_type = payload.get("type") or ev.get("type", "")

            if event_type == "error":
                error = ev.get("error") or payload.get("error") or {}
                if isinstance(error, dict):
                    detail = str(error.get("message") or error.get("code") or error)
                else:
                    detail = str(error)
                failure = RuntimeError(f"OpenAI Realtime error: {detail}")
                logger.error("%s", failure)
                if not self._session_updated.is_set():
                    self._startup_error = failure
                    self._started.set()
                else:
                    await self._emit({"type": "call_error", "message": str(failure)})

            elif event_type == "session.updated":
                updated_session = ev.get("session") or payload.get("session") or {}
                updated_audio = (
                    updated_session.get("audio", {}) if isinstance(updated_session, dict) else {}
                )
                updated_input = (
                    updated_audio.get("input", {}) if isinstance(updated_audio, dict) else {}
                )
                updated_transcription = (
                    updated_input.get("transcription", {})
                    if isinstance(updated_input, dict)
                    else {}
                )
                logger.info(
                    "OpenAI Realtime GA Session updated & confirmed call_id=%s "
                    "transcription_model=%s languages=%s noise_reduction=%s vad=%s",
                    self.transport.session.call_id,
                    (
                        updated_transcription.get("model", "unknown")
                        if isinstance(updated_transcription, dict)
                        else "unknown"
                    ),
                    (
                        updated_transcription.get("languages")
                        or updated_transcription.get("language")
                        if isinstance(updated_transcription, dict)
                        else "unknown"
                    ),
                    (
                        updated_input.get("noise_reduction")
                        if isinstance(updated_input, dict)
                        else "unknown"
                    ),
                    (
                        updated_input.get("turn_detection")
                        if isinstance(updated_input, dict)
                        else "unknown"
                    ),
                )
                self._session_updated.set()
                self._started.set()

            elif event_type == "response.created":
                state = self._response_state(
                    ev,
                    payload,
                    kind=self._creating_response_kind,
                )
                self._active_response_key = state.key
                self._response_phase = _ResponsePhase.IN_PROGRESS
                logger.info(
                    "Realtime response.created response_id=%s kind=%s",
                    state.key,
                    state.kind,
                )
                if self._cancel_on_create:
                    state.interrupted = True
                    self._drop_interrupted_remote_audio = True
                    self._cancel_active_response()
                    self._clear_server_output_buffer()
                    return
                self._drop_interrupted_remote_audio = False
                self._remote_audio_response_key = state.key
                self._remote_response_has_audible_audio = False
                self._remote_audio_resampler = self._new_remote_audio_resampler()
                self._remote_pcm_accumulator.clear()

            elif event_type == "input_audio_buffer.speech_started":
                item_id = self._transcription_item_id(ev, payload) or self._client_event_id(
                    "speech"
                )
                active = self._responses.get(self._active_response_key or "")
                if self._ai_end_call_requested or (
                    active is not None and active.kind == "terminal"
                ):
                    logger.info(
                        "Ignoring overlap during final AI closing item_id=%s",
                        item_id,
                    )
                    return
                self._awaiting_transcription_items.add(item_id)
                self._cancel_caller_turn_settle()
                during_output = self._assistant_output_active()
                if during_output:
                    self._speech_started_during_output.add(item_id)
                    self._schedule_barge_in_confirmation(item_id)
                logger.info(
                    "Realtime caller speech candidate item_id=%s during_output=%s",
                    item_id,
                    during_output,
                )

            elif event_type == "input_audio_buffer.speech_stopped":
                logger.info(
                    "Realtime caller speech stopped item_id=%s; awaiting transcription",
                    self._transcription_item_id(ev, payload),
                )

            elif event_type == "output_audio_buffer.started":
                self._output_buffer_busy = True
                logger.info(
                    "Realtime output audio buffer started response_id=%s",
                    self._response_key(ev, payload),
                )

            elif event_type in (
                "output_audio_buffer.stopped",
                "output_audio_buffer.cleared",
            ):
                self._output_buffer_busy = False
                response_key = self._response_key(ev, payload)
                state = self._responses.get(response_key or self._remote_audio_response_key or "")
                if event_type == "output_audio_buffer.cleared":
                    self._output_buffer_clear_pending = False
                    if state is not None:
                        state.interrupted = True
                        state.output_buffer_terminal.set()
                elif state is not None:
                    state.output_buffer_stopped = True
                    state.output_buffer_terminal.set()
                    self._schedule_audio_finish(state)
                logger.info(
                    "Realtime output audio buffer %s response_id=%s",
                    "cleared" if event_type.endswith("cleared") else "stopped",
                    response_key,
                )
                self._dispatch_pending_if_ready()

            elif event_type in (
                "response.output_audio_transcript.delta",
                "response.audio_transcript.delta",
            ):
                delta = str(ev.get("delta") or payload.get("delta", ""))
                if delta:
                    state = self._response_state(ev, payload)
                    self._capture_output_item_identity(state, ev, payload)
                    state.text += delta
                    self._current_assistant_text = state.text
                    await self._emit(
                        {"type": "transcript_delta", "role": "assistant", "delta": delta}
                    )

            elif event_type == "conversation.item.input_audio_transcription.completed":
                transcript = str(ev.get("transcript") or payload.get("transcript", "")).strip()
                item_id = self._transcription_item_id(ev, payload)
                detected_language, transcription_confidence = self._transcription_metadata(
                    ev, payload
                )
                if item_id:
                    self._awaiting_transcription_items.discard(item_id)
                barge_task = self._barge_in_tasks.pop(item_id or "", None)
                if barge_task is not None and not barge_task.done():
                    barge_task.cancel()
                if transcript:
                    if item_id and item_id in self._processed_transcription_items:
                        logger.info("Ignored duplicate caller transcription item_id=%s", item_id)
                        return
                    if item_id:
                        self._processed_transcription_items.add(item_id)
                    logger.info(
                        "Caller transcript item_id=%s language=%s confidence=%s text=%s",
                        item_id,
                        detected_language or "unknown",
                        (
                            f"{transcription_confidence:.3f}"
                            if transcription_confidence is not None
                            else "unavailable"
                        ),
                        transcript,
                    )
                    quality = self.policy.classify_turn(transcript)
                    explicit_control = self.policy.is_explicit_conversation_control(transcript)
                    low_confidence = bool(
                        transcription_confidence is not None
                        and transcription_confidence < LOW_TRANSCRIPTION_CONFIDENCE
                        and not explicit_control
                    )
                    if low_confidence:
                        logger.warning(
                            "Low-confidence caller transcription item_id=%s confidence=%.3f; "
                            "task state will not be updated",
                            item_id,
                            transcription_confidence,
                        )
                    started_during_output = bool(
                        item_id and item_id in self._speech_started_during_output
                    )
                    needs_response = self._transcript_needs_response(
                        transcript,
                        quality,
                        started_during_output=started_during_output,
                    )
                    if needs_response:
                        if started_during_output and self._assistant_output_active():
                            await self._interrupt_assistant_for_caller(
                                item_id or self._client_event_id("transcription"),
                                reason="chatgpt_actionable_caller_turn",
                            )
                        await self.policy.observe_transcription(
                            transcript,
                            language_code=detected_language,
                            trusted_for_task=not low_confidence,
                            transcription_confidence=transcription_confidence,
                        )
                        self._caller_turn_response_pending = True
                    else:
                        await self._record_ignored_transcription(
                            transcript,
                            item_id=item_id,
                            quality=quality,
                        )
                else:
                    self._transcription_failed_for_turn = True
                    self._caller_turn_response_pending = True
                    logger.warning(
                        "Caller transcription completed empty item_id=%s; model will use "
                        "the original audio or ask for repetition",
                        item_id,
                    )
                if item_id:
                    self._speech_started_during_output.discard(item_id)
                    self._confirmed_barge_in_items.discard(item_id)
                if self._caller_turn_response_pending and not self._awaiting_transcription_items:
                    self._schedule_caller_turn_response()

            elif event_type == "conversation.item.input_audio_transcription.failed":
                item_id = self._transcription_item_id(ev, payload)
                self._transcription_failed_for_turn = True
                self._caller_turn_response_pending = True
                logger.warning(
                    "Caller transcription failed item_id=%s; model will use the original "
                    "audio or ask for repetition",
                    item_id,
                )
                if item_id:
                    self._awaiting_transcription_items.discard(item_id)
                    self._speech_started_during_output.discard(item_id)
                    self._confirmed_barge_in_items.discard(item_id)
                if self._caller_turn_response_pending and not self._awaiting_transcription_items:
                    self._schedule_caller_turn_response()

            elif event_type == "response.output_item.added":
                state = self._response_state(ev, payload)
                self._capture_output_item_identity(state, ev, payload)

            elif event_type in ("response.output_audio.done", "response.audio.done"):
                state = self._response_state(ev, payload)
                self._capture_output_item_identity(state, ev, payload)
                state.audio_done_received = True
                self._schedule_audio_finish(state)

            elif event_type in (
                "response.output_audio_transcript.done",
                "response.audio_transcript.done",
            ):
                state = self._response_state(ev, payload)
                self._capture_output_item_identity(state, ev, payload)
                if not state.text:
                    state.text = str(ev.get("transcript") or payload.get("transcript", ""))
                await self._finalize_response_state(state)

            elif event_type == "response.done":
                state = self._response_state(ev, payload)
                response = ev.get("response") or payload.get("response") or {}
                if not state.text:
                    if isinstance(response, dict):
                        for item in response.get("output", []) or []:
                            if not isinstance(item, dict):
                                continue
                            if item.get("id") and not state.output_item_id:
                                state.output_item_id = str(item["id"])
                            for content_index, content in enumerate(item.get("content", []) or []):
                                if isinstance(content, dict) and content.get("transcript"):
                                    state.output_content_index = content_index
                                    state.text += str(content["transcript"])
                await self._finalize_response_state(state)
                self._complete_response_lifecycle(ev, payload)
                if (
                    state.kind == "terminal"
                    and not state.audio_done_received
                    and not state.playback_closed
                ):
                    message = "Realtime final closing completed without audio"
                    await self._mark_response_audio_failed(state, message)
                    await self._emit({"type": "call_error", "message": message})
                    state.playback_closed = True
                    await self._notify_terminal_completion(
                        f"AI ended call after closing audio failure: {message}"
                    )
                tool_calls = self._collect_tool_calls(response)
                if tool_calls:
                    may_wait_for_operator = any(
                        (self.tool_catalog.get(name) is not None)
                        and self.tool_catalog[name].timeout_secs > 10
                        for _, name, _ in tool_calls
                    )
                    if may_wait_for_operator:
                        self._tasks.append(
                            asyncio.create_task(
                                self._run_tool_calls(tool_calls, source_state=state),
                                name=f"chatgpt-tool-approval-{state.key}",
                            )
                        )
                    else:
                        await self._run_tool_calls(tool_calls, source_state=state)

        except Exception as exc:
            logger.exception("Error handling Realtime DataChannel event: %s", exc)

    @staticmethod
    def _collect_tool_calls(response: Any) -> list[tuple[str, str, str]]:
        if not isinstance(response, dict):
            return []
        calls: list[tuple[str, str, str]] = []
        for item in response.get("output", []) or []:
            if not isinstance(item, dict) or item.get("type") != "function_call":
                continue
            call_id = str(item.get("call_id") or "")
            name = str(item.get("name") or "")
            if call_id and name:
                calls.append((call_id, name, str(item.get("arguments") or "")))
        return calls

    async def _run_tool_calls(
        self,
        calls: list[tuple[str, str, str]],
        *,
        source_state: _RealtimeResponseState | None = None,
    ) -> None:
        returned = 0
        accepted_end_call: dict[str, Any] | None = None
        for call_id, name, raw_arguments in calls:
            grounding = ground_tool_arguments(
                name,
                raw_arguments,
                self.policy.last_caller_text,
                transcript_trusted=self.policy.last_caller_transcript_trusted,
                caller_turns=tuple(self.policy.recent_caller_turns),
            )
            effective_arguments = grounding.raw_arguments
            if grounding.grounded_fields:
                await self._emit(
                    {
                        "type": "tool_arguments_grounded",
                        "name": name,
                        "fields": list(grounding.grounded_fields),
                        "blocked": grounding.blocked,
                    }
                )
            output = (
                grounding.blocked_output()
                if grounding.blocked
                else await execute_tool(self.tool_catalog, name, effective_arguments)
            )
            await self._emit(
                {
                    "type": "tool_call",
                    "name": name,
                    "arguments": effective_arguments,
                    "result": output,
                    "grounded_fields": list(grounding.grounded_fields),
                }
            )
            self.send_event(
                {
                    "type": "conversation.item.create",
                    "event_id": self._client_event_id("tool_result"),
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output,
                    },
                }
            )
            returned += 1
            if name == END_CALL_TOOL_NAME:
                try:
                    result = json.loads(output)
                except json.JSONDecodeError:
                    result = {}
                if isinstance(result, dict) and result.get("accepted") is True:
                    accepted_end_call = result
        if accepted_end_call is not None:
            await self._accept_ai_end_call(accepted_end_call, source_state=source_state)
        elif returned:
            self._request_response(
                {
                    "type": "response.create",
                    "response": {"metadata": {"phoneagent_kind": "tool_result"}},
                },
                kind="tool_result",
            )

    @staticmethod
    def _same_spoken_message(first: str, second: str) -> bool:
        def normalize(value: str) -> str:
            return " ".join(re.sub(r"[^\wÀ-ÿ]+", " ", value.casefold()).split())

        return bool(normalize(first)) and normalize(first) == normalize(second)

    async def _accept_ai_end_call(
        self,
        result: dict[str, Any],
        *,
        source_state: _RealtimeResponseState | None,
    ) -> None:
        if self._terminal_completion_notified or self._ai_end_call_requested:
            return
        reason = str(result.get("reason", "conversation complete")).strip()
        closing_message = str(result.get("closing_message", "Goodbye.")).strip()
        self._ai_end_call_requested = True
        self._ai_end_call_reason = reason
        self._caller_turn_response_pending = False
        self._pending_response = None
        await self._emit(
            {
                "type": "ai_end_call_requested",
                "reason": reason,
                "closing_message": closing_message,
            }
        )
        if (
            source_state is not None
            and source_state.text
            and self._same_spoken_message(source_state.text, closing_message)
        ):
            source_state.kind = "terminal"
            if source_state.playback_closed:
                await self._notify_terminal_completion(f"AI ended call: {reason}")
            elif source_state.audio_end is not None:
                self._start_playback_monitor(source_state)
            return
        self._request_response(
            {
                "type": "response.create",
                "response": {
                    "metadata": {"phoneagent_kind": "terminal"},
                    "tool_choice": "none",
                    "instructions": (
                        "This is the final telephone closing. Speak exactly the quoted sentence "
                        "once, with natural warmth, then produce no additional words and ask no "
                        "question. Exact sentence: "
                        f"{json.dumps(closing_message, ensure_ascii=False)}"
                    ),
                },
            },
            kind="terminal",
        )

    async def _notify_terminal_completion(self, reason: str) -> None:
        if self._terminal_completion_notified:
            return
        self._terminal_completion_notified = True
        await self._emit({"type": "call_completion", "reason": reason})
        sink = self.call_completion_sink
        if sink is None:
            return
        result = sink(reason)
        if inspect.isawaitable(result):
            await result

    def send_event(self, event: dict[str, Any]) -> None:
        """Send a JSON control event over the WebRTC DataChannel."""
        if self.dc and self.dc.readyState == "open":
            self.dc.send(json.dumps(event))

    def add_external_context(self, text: str, *, respond: bool = True) -> None:
        """Inject verified channel context, optionally asking Realtime to respond."""
        event = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }
        self.send_event(event)
        if respond:
            self._request_response({"type": "response.create"})

    def send_text_message(self, text: str) -> None:
        """Inject a text prompt directly into the ongoing voice call."""
        self.add_external_context(text, respond=True)

    async def greet(self) -> None:
        """Trigger outbound sales opening greeting from the voice model."""
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
            name = identity.get("name", "Adam")
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

            logger.info("Triggering Developer Realtime call opening: %s", greeting)
            trigger = {
                "type": "response.create",
                "response": {
                    "instructions": (
                        "The call just connected. "
                        f"Speak your opening greeting now: '{greeting}'"
                    )
                },
            }
            self._request_response(trigger, kind="greeting")
            if self.media_track is not None:
                self.media_track.enable_input()

    async def stop(self, timeout_secs: float = 10.0) -> None:
        """Gracefully shut down WebRTC connection."""
        await self.close()

    async def cancel(self, reason: str) -> None:
        """Cancel and close immediately."""
        logger.info("cancelling ChatGPT Realtime pipeline reason=%s", reason)
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._running = False

        if self.media_track:
            caller_quality = self.media_track.quality_snapshot()
            logger.info("Realtime caller audio quality summary %s", caller_quality)
            await self._emit({"type": "caller_audio_quality", **caller_quality})
            self.transport.remove_audio_listener(self.media_track.push_pcm_frame)
            self.media_track.stop()
        if self.dc:
            self.dc.close()
        if self.pc:
            await self.pc.close()

        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        managed_runtimes = [
            runtime
            for runtime in [self.managed_tool_runtime, *self._retired_managed_tool_runtimes]
            if runtime is not None
        ]
        if managed_runtimes:
            await asyncio.gather(
                *(runtime.close() for runtime in managed_runtimes),
                return_exceptions=True,
            )
        self.managed_tool_runtime = None
        self._retired_managed_tool_runtimes.clear()
        openwa_runtimes = [
            runtime
            for runtime in [self.openwa_runtime, *self._retired_openwa_runtimes]
            if runtime is not None
        ]
        if openwa_runtimes:
            await asyncio.gather(
                *(runtime.close() for runtime in openwa_runtimes),
                return_exceptions=True,
            )
        self.openwa_runtime = None
        self._retired_openwa_runtimes.clear()
        web_research_runtimes = [
            runtime
            for runtime in [
                self.web_research_runtime,
                *self._retired_web_research_runtimes,
            ]
            if runtime is not None
        ]
        if web_research_runtimes:
            await asyncio.gather(
                *(runtime.close() for runtime in web_research_runtimes),
                return_exceptions=True,
            )
        self.web_research_runtime = None
        self._retired_web_research_runtimes.clear()
        frappe_runtimes = [
            runtime
            for runtime in [self.frappe_runtime, *self._retired_frappe_runtimes]
            if runtime is not None
        ]
        if frappe_runtimes:
            await asyncio.gather(
                *(runtime.close() for runtime in frappe_runtimes),
                return_exceptions=True,
            )
        self.frappe_runtime = None
        self._retired_frappe_runtimes.clear()
        await self.mcp_broker.close()

        await self.policy.close()

    async def _emit(self, event: dict[str, Any]) -> None:
        if self.event_sink is None:
            return
        result = self.event_sink(event)
        if inspect.isawaitable(result):
            await result
