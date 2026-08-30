"""Native OpenAI Realtime WebSocket speech-to-speech pipeline.

The cellular bridge is PCM16 mono at 16 kHz. OpenAI Realtime WebSocket audio is
PCM16 mono at 24 kHz. This module keeps both directions as stateful PCM streams,
uses deterministic server VAD for turn creation, and truncates interrupted assistant
audio at the exact duration acknowledged by Android's playout clock.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
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
from typing import Any
from urllib.parse import quote

import av
import numpy as np
from pipecat.frames.frames import OutputAudioRawFrame
from websockets.asyncio.client import connect

from .agent_policy import AgentPolicyRuntime, EventSink
from .chatgpt_realtime_auth import ChatGPTAuthManager
from .duplex_echo_gate import DuplexEchoGate
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
    unimplemented_tools,
)
from .tool_argument_grounding import ground_tool_arguments
from .tool_control import ManagedToolRuntime, ToolControlStore
from .web_research import WebResearchConfigStore, WebResearchToolRuntime

logger = logging.getLogger("OpenAIRealtimeWebSocketPipeline")

PHONE_SAMPLE_RATE = 16_000
REALTIME_SAMPLE_RATE = 24_000
PHONE_FRAME_MS = 20
PHONE_FRAME_BYTES = PHONE_SAMPLE_RATE * PHONE_FRAME_MS // 1000 * 2
REALTIME_FRAME_BYTES = REALTIME_SAMPLE_RATE * PHONE_FRAME_MS // 1000 * 2
INPUT_BATCH_FRAMES = 2
INPUT_QUEUE_BATCHES = 30
STARTUP_STABILIZER_MIN_FRAMES = 8  # 160 ms, within the server's 300 ms VAD prefix.
STARTUP_STABILIZER_MAX_FRAMES = 25  # 500 ms hard bound before transparent pass-through.
STARTUP_STABILIZER_WINDOW_FRAMES = 8
STARTUP_STABILIZER_REQUIRED_VOICE_FRAMES = 6
STARTUP_STABILIZER_VOICE_RMS = 70.0
# Realtime generates faster than the phone renders, so queued speech is normal.
# The queue is deliberately unbounded: blocking a put would stall the WebSocket
# reader, delaying the barge-in and error events that arrive behind the audio
# deltas. Android's credit window remains the real backpressure. This threshold
# only reports an abnormally long turn.
OUTPUT_QUEUE_WARN_FRAMES = 1000
CONTROL_QUEUE_EVENTS = 512
LOW_TRANSCRIPTION_CONFIDENCE = 0.18
PLAYBACK_ACK_TIMEOUT_SECS = 8.0
MAX_CONSECUTIVE_CONNECT_FAILURES = 3
RECONNECT_DELAYS_SECS = (0.25, 0.5, 1.0)
# Replayed on reconnect. A cellular call cannot outrun this in practice and
# it keeps the recovery burst small.
CONVERSATION_REPLAY_TURNS = 40
# Two unanswered check-ins is the limit before silence is treated as the
# caller having left, rather than nagging them.
MAX_IDLE_REENGAGEMENTS = 2
MAX_TERMINAL_RESPONSE_ATTEMPTS = 2

TerminalFailureSink = Callable[[str], Awaitable[None] | None]


def _resample_frame(resampler: av.AudioResampler, pcm: bytes, source_rate: int) -> bytes:
    """Feed one contiguous PCM block through a stateful PyAV resampler."""

    if not pcm:
        return b""
    samples = np.frombuffer(pcm, dtype="<i2")
    if samples.size == 0:
        return b""
    frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
    frame.sample_rate = source_rate
    converted = resampler.resample(frame)
    return b"".join(
        output.to_ndarray().reshape(-1).astype("<i2", copy=False).tobytes() for output in converted
    )


def _flush_resampler(resampler: av.AudioResampler) -> bytes:
    converted = resampler.resample(None)
    return b"".join(
        output.to_ndarray().reshape(-1).astype("<i2", copy=False).tobytes() for output in converted
    )


class _StartupSpeechVerifier:
    """Observe initial PCM for sustained human energy without modifying audio."""

    def __init__(self) -> None:
        self._observed_frames = 0
        self._voice_flags: list[bool] = []
        self._settled = False
        self._human_confirmed = False

    @staticmethod
    def _rms(pcm: bytes) -> float:
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        return float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0

    def observe(self, frame: bytes) -> None:
        if self._settled:
            return
        self._observed_frames += 1
        self._voice_flags.append(self._rms(frame) >= STARTUP_STABILIZER_VOICE_RMS)
        recent = self._voice_flags[-STARTUP_STABILIZER_WINDOW_FRAMES:]
        sustained_voice = (
            self._observed_frames >= STARTUP_STABILIZER_MIN_FRAMES
            and sum(recent) >= STARTUP_STABILIZER_REQUIRED_VOICE_FRAMES
        )
        if sustained_voice:
            self._settled = True
            self._human_confirmed = True
            self._voice_flags = []
            return
        if self._observed_frames >= STARTUP_STABILIZER_MAX_FRAMES:
            self._settled = True
            self._voice_flags = []

    def reset(self) -> None:
        self._observed_frames = 0
        self._voice_flags.clear()
        self._settled = False
        self._human_confirmed = False

    @property
    def human_confirmed(self) -> bool:
        return self._human_confirmed

    def snapshot(self) -> dict[str, int | bool]:
        return {
            "startup_verifier_observed_frames": self._observed_frames,
            "startup_verifier_human_confirmed": self._human_confirmed,
            "startup_verifier_settled": self._settled,
        }


class _PhoneInputBridge:
    """Thread-safe, non-destructive 16 kHz -> 24 kHz caller audio bridge."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._loop_thread_id = threading.get_ident()
        self._running = True
        self._enabled = False
        self._raw = bytearray()
        self._batch = bytearray()
        self._source_frames_in_batch = 0
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=INPUT_QUEUE_BATCHES)
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=REALTIME_SAMPLE_RATE)
        self._frames = 0
        self._queue_drops = 0
        self._silence_frames = 0
        self._speech_frames = 0
        self._total_samples = 0
        self._clipped_samples = 0
        self._rms_sum = 0.0
        self._peak = 0
        self._echo_gate = DuplexEchoGate()
        self._startup_verifier = _StartupSpeechVerifier()

    def push_pcm_frame(self, pcm: bytes) -> None:
        if not self._running or not pcm:
            return
        if threading.get_ident() == self._loop_thread_id:
            self._offer(pcm)
            return
        try:
            self._loop.call_soon_threadsafe(self._offer, pcm)
        except RuntimeError:
            pass

    def _offer(self, pcm: bytes) -> None:
        if not self._running or not self._enabled:
            return
        self._raw.extend(pcm)
        while len(self._raw) >= PHONE_FRAME_BYTES:
            frame = bytes(self._raw[:PHONE_FRAME_BYTES])
            del self._raw[:PHONE_FRAME_BYTES]
            self._observe(frame)
            self._startup_verifier.observe(frame)
            for sanitized in self._echo_gate.process_input_frame(frame):
                self._batch.extend(
                    _resample_frame(self._resampler, sanitized, PHONE_SAMPLE_RATE)
                )
                self._source_frames_in_batch += 1
                if self._source_frames_in_batch >= INPUT_BATCH_FRAMES:
                    # Batch by source time, not resampler output size. A stateful
                    # filter retains a few startup samples internally; waiting for
                    # an exact byte count would add a full 20 ms to the first turn.
                    if self._batch:
                        self._put(bytes(self._batch))
                    self._batch.clear()
                    self._source_frames_in_batch = 0

    def _put(self, pcm: bytes) -> None:
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
                self._queue_drops += INPUT_BATCH_FRAMES
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(pcm)

    def _observe(self, pcm: bytes) -> None:
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        if samples.size == 0:
            return
        rms = float(np.sqrt(np.mean(samples * samples)))
        peak = int(np.max(np.abs(samples)))
        self._frames += 1
        self._total_samples += int(samples.size)
        self._rms_sum += rms
        self._peak = max(self._peak, peak)
        self._clipped_samples += int(np.count_nonzero(np.abs(samples) >= 32760.0))
        if rms < 8.0:
            self._silence_frames += 1
        elif rms >= 70.0:
            self._speech_frames += 1

    def enable(self) -> None:
        self.discard_pending()
        self._enabled = True
        logger.info("Realtime caller audio enabled after greeting dispatch")

    def note_output_pcm(self, pcm: bytes) -> None:
        self._echo_gate.note_output_pcm(pcm)

    def set_assistant_playback(self, active: bool) -> None:
        self._echo_gate.set_playback_active(active)

    def has_recent_human_speech(self) -> bool:
        return (
            self._startup_verifier.human_confirmed
            or self._echo_gate.has_recent_human_speech()
        )

    def discard_pending(self) -> None:
        """Drop audio that cannot safely cross a Realtime connection boundary."""

        self._raw.clear()
        self._batch.clear()
        self._source_frames_in_batch = 0
        self._echo_gate.discard_pending_input()
        self._startup_verifier.reset()
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break

    def stop(self) -> None:
        self._running = False
        self._enabled = False
        self._echo_gate.reset()

    def quality_snapshot(self) -> dict[str, int | float]:
        frames = max(1, self._frames)
        samples = max(1, self._total_samples)
        quality: dict[str, int | float] = {
            "caller_input_frames": self._frames,
            "caller_input_queue_drops": self._queue_drops,
            "caller_input_mean_rms": round(self._rms_sum / frames, 1),
            "caller_input_peak": self._peak,
            "caller_input_speech_frame_pct": round(self._speech_frames * 100 / frames, 1),
            "caller_input_silence_frame_pct": round(self._silence_frames * 100 / frames, 1),
            "caller_input_clipped_sample_pct": round(self._clipped_samples * 100 / samples, 4),
        }
        quality.update(self._echo_gate.snapshot())
        quality.update(self._startup_verifier.snapshot())
        return quality


@dataclass(frozen=True, slots=True)
class _OutputQueueItem:
    response_key: str
    pcm: bytes | None = None


@dataclass(slots=True)
class _ResponseState:
    key: str
    kind: str
    text: str = ""
    output_item_id: str | None = None
    content_index: int = 0
    # Phone-render identity of this response's own first frame. The session's
    # output sequence is global and monotonic across the whole call, and a
    # barge-in flush discards frames that already consumed sequence numbers.
    # Differencing a call-global counter therefore over-counts every later
    # response by the number of frames previously thrown away.
    first_output_sequence: int | None = None
    frames_written: int = 0
    audio_ms_generated: float = 0.0
    created_at: float = field(default_factory=time.monotonic)
    first_audio_at: float | None = None
    interrupted: bool = False
    interrupted_by: str = "caller"
    cancel_sent: bool = False
    truncate_sent: bool = False
    pending_truncate_ms: int | None = None
    audio_done: bool = False
    marker_queued: bool = False
    audio_end: tuple[int, int] | None = None
    playback_closed: bool = False
    monitor_started: bool = False
    response_status: str = "in_progress"
    status_details: dict[str, Any] | None = None
    suppress_transcript: bool = False
    output_tokens: int | None = None
    audio_output_tokens: int | None = None
    finalized: asyncio.Event = field(default_factory=asyncio.Event)
    pcm_accumulator: bytearray = field(default_factory=bytearray)
    resampler: av.AudioResampler = field(
        default_factory=lambda: av.AudioResampler(
            format="s16", layout="mono", rate=PHONE_SAMPLE_RATE
        )
    )


@dataclass(slots=True)
class _CallerTurnState:
    item_id: str
    stopped: bool = False
    transcript_seen: bool = False
    discarded: bool = False
    guarded_opening: bool = False
    human_confirmed: bool = False


class OpenAIRealtimeWebSocketPipeline:
    """One production S2S call over OpenAI's server-to-server WebSocket API."""

    def __init__(
        self,
        transport: PhoneAgentTransport,
        config: RuntimeConfig,
        *,
        auth_manager: ChatGPTAuthManager | None = None,
        caller_id: str = "anonymous",
        call_direction: str = "outbound",
        event_sink: EventSink | None = None,
        terminal_failure_sink: TerminalFailureSink | None = None,
        call_completion_sink: TerminalFailureSink | None = None,
        mcp_broker: McpToolBroker | None = None,
    ) -> None:
        self.transport = transport
        self.config = config
        self.auth_manager = auth_manager or ChatGPTAuthManager()
        self.caller_id = caller_id
        self.event_sink = event_sink
        self.terminal_failure_sink = terminal_failure_sink
        self.call_completion_sink = call_completion_sink
        providers = config.providers
        requested_model = (providers.chatgpt_realtime_model or "auto").strip().lower()
        self.model = "gpt-realtime-2.1" if requested_model == "auto" else requested_model
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,80}", self.model):
            raise ValueError(f"Invalid Realtime model name: {self.model!r}")
        self.voice = providers.chatgpt_realtime_voice
        self.policy = AgentPolicyRuntime(
            caller_id=caller_id,
            task_id=config.task_id,
            language=providers.stt_language,
            call_direction=call_direction,
            additional_instructions=config.system_prompt,
            # Realtime remains a clean call: compile_realtime never injects
            # account or prior caller context. Local verified turns are still
            # written asynchronously after delivery so future, operator-
            # approved memory workflows have durable evidence.
            memory_enabled=config.memory_enabled,
            event_sink=event_sink,
        )
        self.tool_catalog = build_tool_catalog(self.policy.task_contract, self.policy.task)
        # Call completion is a core conversational control, not a task-specific
        # business permission. The model owns the semantic decision; this local
        # tool only carries that decision to the telephony host safely.
        self.tool_catalog[END_CALL_TOOL_NAME] = build_end_call_tool()
        self.policy.task_contract["allowed_tools"] = sorted(
            {
                str(name)
                for name in self.policy.task_contract.get("allowed_tools", []) or []
            }
            | {END_CALL_TOOL_NAME}
        )
        identity_skill_tool = self.policy.persona_compiler.identity_kernel.realtime_skill_tool(
            task_id=self.policy.task_id,
            language=providers.stt_language,
            authorized_tools={
                str(name) for name in self.policy.task_contract.get("allowed_tools", []) or []
            },
        )
        if identity_skill_tool is not None:
            self.tool_catalog[identity_skill_tool.name] = identity_skill_tool
        self._contract_allowed_tools = {
            str(name) for name in self.policy.task_contract.get("allowed_tools", []) or []
        }
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
        self._response_idle = asyncio.Event()
        self._response_idle.set()
        self.mcp_broker = mcp_broker or McpToolBroker.from_environment(
            task_allowed_tools={
                str(name) for name in self.policy.task_contract.get("allowed_tools", []) or []
            },
            call_id=self.transport.session.call_id,
        )
        self.policy.available_tools = set(self.tool_catalog)
        self._base_instructions = self.policy.persona_compiler.compile_realtime(
            task_contract=self.policy.task_contract,
            language=providers.stt_language,
            additional_instructions=config.system_prompt,
            available_tools=self.policy.available_tools,
            caller_id=self.policy.caller_id,
            call_direction=self.policy.call_context.direction.value,
        )
        self.ws: Any | None = None
        self.input_bridge: _PhoneInputBridge | None = None
        self._control_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=CONTROL_QUEUE_EVENTS
        )
        self._output_queue: asyncio.Queue[_OutputQueueItem] = asyncio.Queue()
        self._output_queue_warned = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._session_updated = asyncio.Event()
        self._connection_ready = asyncio.Event()
        self._connected = asyncio.Event()
        self._connection_error: Exception | None = None
        self._startup_error: Exception | None = None
        self._running = False
        self._closed = False
        self._greeted = False
        self._greet_lock = asyncio.Lock()
        self._creating_kind: str | None = None
        self._active_response_key: str | None = None
        self._generating_response_key: str | None = None
        self._responses: dict[str, _ResponseState] = {}
        self._implicit_response_sequence = 0
        self._last_speech_stopped_at: float | None = None
        self._last_speech_item_id: str | None = None
        self._processed_transcriptions: set[str] = set()
        self._caller_turns: dict[str, _CallerTurnState] = {}
        self._discarded_caller_items: set[str] = set()
        self._connection_generation = 0
        self._terminal_failure_notified = False
        self._terminal_completion_notified = False
        self._pending_terminal_instruction: str | None = None
        self._terminal_instruction: str | None = None
        self._terminal_response_attempts = 0
        self._ai_end_call_requested = False
        self._ai_end_call_reason: str | None = None
        self._pending_tool_response = False
        self._opening_vad_guard_active = False
        self._opening_vad_restore_pending = False
        self._opening_guard_items: set[str] = set()
        self._opening_confirmation_tasks: dict[str, asyncio.Task[Any]] = {}
        self._pending_guarded_turn_response = False
        self._terminal_override_active = False
        # A reconnect starts an empty server-side conversation. Without this the
        # model resumes the call with no memory of anything already said.
        self._conversation_log: list[tuple[str, str]] = []
        self._idle_reengagements = 0

    def _refresh_tool_instructions(self) -> None:
        self.policy.available_tools = set(self.tool_catalog)
        self._base_instructions = self.policy.persona_compiler.compile_realtime(
            task_contract=self.policy.task_contract,
            language=self.config.providers.stt_language,
            additional_instructions=self.config.system_prompt,
            available_tools=self.policy.available_tools,
            caller_id=self.policy.caller_id,
            call_direction=self.policy.call_context.direction.value,
        )

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
        """Hot-apply operator tool activation without touching the live media path."""

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
                await self._emit(
                    {"type": "tools_reload_failed", "message": str(exc)[:500]}
                )

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
        """Build a reviewed catalog first, then atomically publish it to Realtime."""

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
            # A handler from the just-finished model response may still hold a
            # reference to the previous runtime. Retire it at call close rather
            # than interrupting an in-flight tool operation.
            if previous is not None:
                self._retired_managed_tool_runtimes.append(previous)

            if update_session and self._running and self._connected.is_set():
                accepted = self.send_event(
                    {
                        "type": "session.update",
                        "event_id": self._event_id("managed_tools"),
                        "session": {
                            "type": "realtime",
                            "instructions": self._session_instructions(),
                            "tools": tool_definitions(self.tool_catalog),
                            "tool_choice": "auto",
                        },
                    }
                )
                if not accepted:
                    raise RuntimeError("Realtime connection did not accept the tool update")
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
            if update_session and self._running and self._connected.is_set():
                if not self.send_event(
                    {
                        "type": "session.update",
                        "event_id": self._event_id("openwa_tools"),
                        "session": {
                            "type": "realtime",
                            "instructions": self._session_instructions(),
                            "tools": tool_definitions(self.tool_catalog),
                            "tool_choice": "auto",
                        },
                    }
                ):
                    raise RuntimeError("Realtime connection did not accept the OpenWA update")
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
            if update_session and self._running and self._connected.is_set():
                if not self.send_event(
                    {
                        "type": "session.update",
                        "event_id": self._event_id("web_research_tools"),
                        "session": {
                            "type": "realtime",
                            "instructions": self._session_instructions(),
                            "tools": tool_definitions(self.tool_catalog),
                            "tool_choice": "auto",
                        },
                    }
                ):
                    raise RuntimeError(
                        "Realtime connection did not accept the web research update"
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
            if update_session and self._running and self._connected.is_set():
                if not self.send_event(
                    {
                        "type": "session.update",
                        "event_id": self._event_id("frappe_tools"),
                        "session": {
                            "type": "realtime",
                            "instructions": self._session_instructions(),
                            "tools": tool_definitions(self.tool_catalog),
                            "tool_choice": "auto",
                        },
                    }
                ):
                    raise RuntimeError("Realtime connection did not accept the Frappe update")
            await self._emit(
                {
                    "type": "frappe_tools_reloaded",
                    "revision": config.revision,
                    "active_tools": sorted(self._frappe_tool_names),
                    "live": bool(update_session),
                }
            )

    async def _inject_openwa_context(self, text: str, respond: bool) -> None:
        if self._generating_response_key:
            try:
                await asyncio.wait_for(self._response_idle.wait(), timeout=10.0)
            except TimeoutError:
                pass
        self.add_external_context(text, respond=respond)

    def _build_transcription(self) -> dict[str, Any]:
        providers = self.config.providers
        languages = list(providers.chatgpt_realtime_input_languages)
        # The transcription "keywords" field is accepted but not applied by the
        # configured models: the acknowledged session echoes only model,
        # language(s) and prompt. Domain vocabulary therefore lives in the
        # prompt, which is honoured.
        transcription: dict[str, Any] = {
            "model": providers.chatgpt_realtime_transcription_model,
            "prompt": (
                "Natural English or French cellular call. Preserve names, numbers and "
                "complete meaning. Domain terms: OXzoon, IPTV, live TV, télévision en direct, "
                "football, smart TV, Firestick, streaming subscription, abonnement."
            ),
        }
        if len(languages) == 1:
            transcription["language"] = languages[0]
        else:
            transcription["languages"] = languages
        return transcription

    def _session_instructions(self) -> str:
        return f"{self._base_instructions}\n\n{self.policy.live_state_instructions()}"

    def _build_turn_detection(self, *, automatic_response: bool = True) -> dict[str, Any]:
        providers = self.config.providers
        common: dict[str, Any] = {
            # The Realtime model owns turn-taking: when to answer and when to
            # stop for the caller. Carrier echo is removed from the uplink by
            # DuplexEchoGate before it can reach VAD, so turn authority does not
            # need to be second-guessed here. PhoneAgent still owns the phone
            # playout clock, which the server cannot observe, so it flushes
            # Android and sends the exact conversation.item.truncate itself.
            "create_response": automatic_response,
            "interrupt_response": automatic_response,
        }
        if providers.chatgpt_realtime_vad_mode == "semantic_vad":
            return {
                "type": "semantic_vad",
                "eagerness": providers.chatgpt_realtime_vad_eagerness,
                **common,
            }
        server_vad: dict[str, Any] = {
            "type": "server_vad",
            "threshold": providers.chatgpt_realtime_vad_threshold,
            "prefix_padding_ms": providers.chatgpt_realtime_vad_prefix_ms,
            "silence_duration_ms": providers.chatgpt_realtime_vad_silence_ms,
            **common,
        }
        if providers.chatgpt_realtime_idle_timeout_ms:
            server_vad["idle_timeout_ms"] = providers.chatgpt_realtime_idle_timeout_ms
        return server_vad

    def _set_turn_detection_automation(self, *, enabled: bool) -> bool:
        return self.send_event(
            {
                "type": "session.update",
                "event_id": self._event_id(
                    "opening_vad_restore" if enabled else "opening_vad_guard"
                ),
                "session": {
                    "type": "realtime",
                    "audio": {
                        "input": {
                            "turn_detection": self._build_turn_detection(
                                automatic_response=enabled
                            )
                        }
                    },
                },
            }
        )

    def _build_session_update(self) -> dict[str, Any]:
        providers = self.config.providers
        noise_reduction: dict[str, str] | None = None
        if providers.chatgpt_realtime_noise_reduction != "off":
            noise_reduction = {"type": providers.chatgpt_realtime_noise_reduction}
        return {
            "type": "session.update",
            "event_id": self._event_id("session"),
            "session": {
                "type": "realtime",
                "model": self.model,
                "instructions": self._session_instructions(),
                "output_modalities": ["audio"],
                # Realtime audio consumes audio output tokens as well as text
                # tokens. A text-style numeric cap can cut speech mid-sentence.
                "max_output_tokens": "inf",
                "reasoning": {"effort": providers.chatgpt_realtime_reasoning_effort},
                "include": ["item.input_audio_transcription.logprobs"],
                "tools": tool_definitions(self.tool_catalog),
                "tool_choice": "auto",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": REALTIME_SAMPLE_RATE},
                        "noise_reduction": noise_reduction,
                        "transcription": self._build_transcription(),
                        "turn_detection": self._build_turn_detection(),
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": REALTIME_SAMPLE_RATE},
                        "voice": self.voice,
                        "speed": providers.chatgpt_realtime_speed,
                    },
                },
            },
        }

    @staticmethod
    def _event_id(prefix: str) -> str:
        return f"phoneagent_{prefix}_{uuid.uuid4().hex}"

    def _authorization_token(self, *, force_refresh: bool = False) -> str:
        api_key = self.config.providers.openai_api_key.strip()
        if api_key:
            return api_key
        return self.auth_manager.get_token(force_refresh=force_refresh)

    async def start(self, timeout_secs: float = 20.0) -> None:
        if self._running:
            return
        self._running = True
        self._closed = False
        try:
            mcp_tools = await self.mcp_broker.start()
            if mcp_tools:
                collisions = set(self.tool_catalog) & set(mcp_tools)
                if collisions:
                    raise RuntimeError("MCP tool name collision after namespace mapping")
                self.tool_catalog.update(mcp_tools)
                self._refresh_tool_instructions()
                await self._emit({"type": "mcp_tools_ready", "count": len(mcp_tools)})
            await self._reload_managed_tools(update_session=False)
            await self._reload_openwa(update_session=False)
            await self._reload_web_research(update_session=False)
            loop = asyncio.get_running_loop()
            self.input_bridge = _PhoneInputBridge(loop)
            self.transport.add_audio_listener(self.input_bridge.push_pcm_frame)
            self.transport.add_output_audio_listener(self.input_bridge.note_output_pcm)
            logger.info(
                "starting OpenAI Realtime WebSocket call_id=%s model=%s voice=%s vad=%s",
                self.transport.session.call_id,
                self.model,
                self.voice,
                self.config.providers.chatgpt_realtime_vad_mode,
            )
            self._spawn(self._input_audio_loop(), "realtime-ws-input")
            self._spawn(self._output_audio_loop(), "realtime-phone-output")
            self._spawn(self._connection_supervisor(), "realtime-ws-supervisor")
            self._spawn(self._managed_tool_watcher(), "realtime-managed-tool-watcher")
            self._spawn(self._openwa_watcher(), "realtime-openwa-watcher")
            self._spawn(self._web_research_watcher(), "realtime-web-research-watcher")
            self._spawn(self._frappe_watcher(), "realtime-frappe-watcher")
            await asyncio.wait_for(self._session_updated.wait(), timeout=timeout_secs)
            if self._startup_error is not None:
                raise self._startup_error
            logger.info(
                "OpenAI Realtime WebSocket ready call_id=%s", self.transport.session.call_id
            )
        except Exception:
            await self.close()
            raise

    def _spawn(self, coro: Any, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)

        def completed(finished: asyncio.Task[Any]) -> None:
            self._tasks.discard(finished)
            if finished.cancelled():
                return
            try:
                failure = finished.exception()
            except asyncio.CancelledError:
                return
            if failure is not None:
                logger.error(
                    "Realtime background task failed task=%s: %s",
                    finished.get_name(),
                    failure,
                    exc_info=(type(failure), failure, failure.__traceback__),
                )

        task.add_done_callback(completed)
        return task

    def send_event(self, event: dict[str, Any]) -> bool:
        """Queue one client event, reporting whether it was actually accepted.

        A dropped event is not cosmetic: silently discarding a truncate or a
        delete while the socket is down, but recording it as sent, leaves the
        model's conversation holding audio the caller never heard.
        """

        if not self._running or not self._connected.is_set():
            logger.warning(
                "Realtime client event dropped while disconnected type=%s",
                event.get("type"),
            )
            return False
        try:
            self._control_queue.put_nowait(event)
        except asyncio.QueueFull:
            failure = RuntimeError("Realtime control queue overflow")
            logger.error("%s", failure)
            self._startup_error = failure
            self._session_updated.set()
            return False
        return True

    async def _connection_supervisor(self) -> None:
        consecutive_failures = 0
        while self._running:
            generation_before = self._connection_generation
            try:
                await self._run_connection(reconnecting=generation_before > 0)
                if self._running:
                    raise ConnectionError("OpenAI Realtime WebSocket ended without a close reason")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    return
                connected_during_attempt = self._connection_generation > generation_before
                consecutive_failures = 0 if connected_during_attempt else consecutive_failures + 1
                self._connected.clear()
                self._connection_ready.clear()
                await self._handle_connection_loss(exc)
                if consecutive_failures >= MAX_CONSECUTIVE_CONNECT_FAILURES:
                    message = (
                        "OpenAI Realtime connection could not be recovered after "
                        f"{consecutive_failures} attempts: {exc}"
                    )
                    await self._fatal(message, terminal=True)
                    return
                attempt_number = consecutive_failures + 1
                delay = RECONNECT_DELAYS_SECS[
                    min(attempt_number - 1, len(RECONNECT_DELAYS_SECS) - 1)
                ]
                logger.warning(
                    "Realtime WebSocket reconnect scheduled attempt=%d delay_ms=%.0f error=%s",
                    attempt_number,
                    delay * 1000,
                    exc,
                )
                await self._emit(
                    {
                        "type": "realtime_reconnecting",
                        "attempt": attempt_number,
                        "delay_ms": round(delay * 1000),
                        "message": str(exc),
                    }
                )
                await asyncio.sleep(delay)

    async def _warn_once_about_missing_tools(self) -> None:
        """A contract that promises a tool nobody wrote makes the agent bluff.

        This is how the agent came to offer a caller a checkout it had no way to
        perform: send_checkout_link was in allowed_tools with nothing behind it.
        """

        missing = unimplemented_tools(self.policy.task_contract, self.tool_catalog)
        if not missing:
            return
        message = (
            f"Task contract allows {', '.join(missing)} but no implementation is loaded; "
            "the agent cannot perform these. Add them under ~/.config/phone-agent/tools/ "
            "or remove them from allowed_tools."
        )
        logger.warning("%s", message)
        await self._emit({"type": "call_notice", "message": message})

    async def _run_connection(self, *, reconnecting: bool) -> None:
        token = await asyncio.to_thread(self._authorization_token)
        if not reconnecting:
            await self._warn_once_about_missing_tools()
        safety_identifier = hashlib.sha256(self.caller_id.encode("utf-8")).hexdigest()
        url = f"wss://api.openai.com/v1/realtime?model={quote(self.model, safe='-._')}"
        headers = {
            "Authorization": f"Bearer {token}",
            "OpenAI-Safety-Identifier": f"phoneagent-{safety_identifier[:48]}",
        }
        logger.info(
            "connecting OpenAI Realtime WebSocket call_id=%s reconnecting=%s",
            self.transport.session.call_id,
            reconnecting,
        )
        ws = await connect(
            url,
            additional_headers=headers,
            compression=None,
            open_timeout=15,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,
            max_queue=1024,
        )
        self.ws = ws
        self._connection_ready = asyncio.Event()
        self._connection_error = None
        reader = asyncio.create_task(
            self._reader_loop(ws), name=f"realtime-ws-reader-{self._connection_generation + 1}"
        )
        writer = asyncio.create_task(
            self._writer_loop(ws), name=f"realtime-ws-writer-{self._connection_generation + 1}"
        )
        ready_waiter = asyncio.create_task(
            self._connection_ready.wait(), name="realtime-session-ready"
        )
        try:
            await ws.send(
                json.dumps(
                    self._build_session_update(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            done, _ = await asyncio.wait(
                {reader, writer, ready_waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if ready_waiter not in done:
                await self._raise_connection_task_failure(reader, writer)
            if self._connection_error is not None:
                raise self._connection_error
            self._connection_generation += 1
            self._connected.set()
            self._session_updated.set()
            logger.info(
                "OpenAI Realtime session ready generation=%d reconnecting=%s",
                self._connection_generation,
                reconnecting,
            )
            replayed = self._replay_conversation() if reconnecting else 0
            if reconnecting and self._pending_terminal_instruction is not None:
                self._dispatch_pending_terminal_response()
                await self._emit(
                    {"type": "realtime_reconnected", "generation": self._connection_generation}
                )
            elif reconnecting and self._greeted:
                self._resume_after_reconnect(replayed)
                await self._emit(
                    {
                        "type": "realtime_reconnected",
                        "generation": self._connection_generation,
                        "replayed_turns": replayed,
                    }
                )
                await self._emit(
                    {
                        "type": "call_notice",
                        "message": f"Reconnected; {replayed} conversation turn(s) restored",
                    }
                )
            await self._raise_connection_task_failure(reader, writer)
        finally:
            self._connected.clear()
            ready_waiter.cancel()
            for task in (reader, writer):
                if not task.done():
                    task.cancel()
            await asyncio.gather(reader, writer, ready_waiter, return_exceptions=True)
            if self.ws is ws:
                self.ws = None
            try:
                await ws.close()
            except Exception:
                pass

    def _resume_after_reconnect(self, replayed: int) -> None:
        """Pick up the restored conversation without talking over the caller.

        Asking the caller to repeat themselves only makes sense when the
        conversation could not be restored. With the history replayed, that
        request contradicts what the model is holding and costs the caller a
        turn. And when the caller is the one who owes the next word, the right
        move after a blip is to say nothing at all and keep listening.
        """

        if not replayed:
            instructions = (
                "The line dropped and is back, and the earlier conversation could not be "
                "restored. Apologize in one short sentence and ask the caller to repeat "
                "only their last sentence, then wait. Do not reintroduce yourself or the "
                "company."
            )
        else:
            last_role, last_text = self._conversation_log[-1]
            assistant_was_cut = last_role == "assistant" and "line dropped" in last_text
            if last_role == "assistant" and not assistant_was_cut:
                # The caller holds the floor. Server VAD will bring them back.
                logger.info(
                    "Realtime resumed after reconnect with %d turns replayed; "
                    "staying silent because it is the caller's turn",
                    replayed,
                )
                return
            if assistant_was_cut:
                instructions = (
                    "The line dropped mid-sentence and is back. The conversation so far is "
                    "already restored above. Finish only the thought you were cut off in, "
                    "briefly. Do not apologize at length, do not ask the caller to repeat "
                    "anything, and do not reintroduce yourself or the company."
                )
            else:
                instructions = (
                    "The line dropped for a moment and is back. The conversation so far is "
                    "already restored above, including the caller's last message, which you "
                    "have not answered yet. Answer it now, directly. Do not ask them to "
                    "repeat it, do not dwell on the connection, and do not reintroduce "
                    "yourself or the company."
                )
        self._creating_kind = "recovery"
        self.send_event(
            {
                "type": "response.create",
                "event_id": self._event_id("recovery"),
                "response": {
                    "output_modalities": ["audio"],
                    "metadata": {"phoneagent_kind": "recovery"},
                    "instructions": instructions,
                },
            }
        )

    @staticmethod
    async def _raise_connection_task_failure(
        reader: asyncio.Task[Any], writer: asyncio.Task[Any]
    ) -> None:
        done, _ = await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
        task = next(iter(done))
        if task.cancelled():
            raise asyncio.CancelledError
        failure = task.exception()
        if failure is not None:
            raise failure
        raise ConnectionError(f"{task.get_name()} ended unexpectedly")

    async def _writer_loop(self, ws: Any) -> None:
        while self._running:
            event = await self._control_queue.get()
            try:
                if event is None:
                    return
                await ws.send(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            finally:
                self._control_queue.task_done()

    async def _reader_loop(self, ws: Any) -> None:
        async for message in ws:
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            await self._handle_event(json.loads(message))

    async def _handle_connection_loss(self, exc: Exception) -> None:
        logger.warning(
            "OpenAI Realtime connection lost generation=%d error_type=%s error=%s",
            self._connection_generation,
            type(exc).__name__,
            exc,
        )
        await self._emit(
            {
                "type": "call_notice",
                "message": (
                    f"Realtime connection lost ({type(exc).__name__}: {exc}); reconnecting"
                ),
            }
        )
        self._drain_control_queue()
        self._generating_response_key = None
        if self.input_bridge is not None:
            self.input_bridge.discard_pending()
        state = self._responses.get(self._active_response_key or "")
        if state is None or state.playback_closed:
            self._creating_kind = None
            self._active_response_key = None
            return
        if (
            state.kind == "terminal"
            and self._terminal_instruction
            and not self._terminal_completion_notified
        ):
            # The AI's decision survives a transient Realtime disconnect. The
            # fresh session will replay one final closing instead of resuming a
            # conversation that the model already decided was complete.
            self._pending_terminal_instruction = self._terminal_instruction
            self._terminal_override_active = True
        state.interrupted = True
        state.interrupted_by = "connection"
        self._discard_queued_output()
        state.pcm_accumulator.clear()
        self.transport.output().discard_audio_segment()
        await self.policy.mark_playback_interrupted()
        started = time.monotonic()
        try:
            await self.transport.coordinator.interrupt(
                "openai_realtime_connection_lost",
                self.transport.output()._flush_phone,
            )
        except Exception:
            logger.warning("phone flush failed during Realtime recovery", exc_info=True)
        delivered_frames = self._delivered_frames(state)
        if not state.playback_closed:
            await self.policy.playback_stopped(delivered_frames=delivered_frames)
            state.playback_closed = True
        logger.info(
            "Realtime interrupted stale output for reconnect response_id=%s flush_ms=%.1f",
            state.key,
            (time.monotonic() - started) * 1000,
        )
        self._creating_kind = None
        self._active_response_key = None

    def _drain_control_queue(self) -> None:
        while not self._control_queue.empty():
            try:
                self._control_queue.get_nowait()
                self._control_queue.task_done()
            except asyncio.QueueEmpty:
                break

    async def _input_audio_loop(self) -> None:
        assert self.input_bridge is not None
        while self._running:
            pcm = await self.input_bridge.queue.get()
            try:
                if not self._connected.is_set():
                    continue
                self.send_event(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(pcm).decode("ascii"),
                    }
                )
            finally:
                self.input_bridge.queue.task_done()

    async def _output_audio_loop(self) -> None:
        while self._running:
            item = await self._output_queue.get()
            try:
                state = self._responses.get(item.response_key)
                if state is None or state.interrupted:
                    continue
                if item.pcm is None:
                    state.audio_end = await self.transport.output().finish_audio_segment()
                    if state.audio_end is None:
                        await self._response_audio_failed(
                            state, "Realtime response ended without phone-deliverable audio"
                        )
                    else:
                        self._start_playback_monitor(state)
                    continue
                result = await self.transport.output().write_audio_frame_result(
                    OutputAudioRawFrame(
                        audio=item.pcm,
                        sample_rate=PHONE_SAMPLE_RATE,
                        num_channels=1,
                    )
                )
                if result is AudioWriteResult.DELIVERED:
                    # Anchor this response to its own first phone frame so the
                    # delivered count can never inherit frames that an earlier
                    # barge-in flush discarded.
                    if state.first_output_sequence is None:
                        state.first_output_sequence = (
                            self.transport.session.metrics.last_output_sequence
                        )
                    state.frames_written += 1
                elif result is AudioWriteResult.CANCELLED:
                    state.interrupted = True
                elif result is AudioWriteResult.FAILED:
                    await self._response_audio_failed(
                        state, "Generated Realtime audio could not reach Android playout"
                    )
                    await self._emit(
                        {
                            "type": "call_error",
                            "message": "Generated Realtime audio could not reach the phone",
                        }
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state = self._responses.get(item.response_key)
                if state is not None:
                    await self._response_audio_failed(state, str(exc))
            finally:
                self._output_queue.task_done()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", ""))
        if event_type == "error":
            error = event.get("error", {})
            detail = error.get("message") or error.get("code") or str(error)
            logger.error(
                "OpenAI Realtime error code=%s event_id=%s message=%s",
                error.get("code"),
                error.get("event_id"),
                detail,
            )
            if not self._connection_ready.is_set():
                self._connection_error = RuntimeError(f"OpenAI Realtime error: {detail}")
                self._connection_ready.set()
            else:
                await self._emit({"type": "call_error", "message": str(detail)})
            return
        if event_type == "session.created":
            logger.info(
                "Realtime session created id=%s",
                (event.get("session") or {}).get("id"),
            )
            return
        if event_type == "session.updated":
            self._connection_ready.set()
            return
        if event_type == "rate_limits.updated":
            logger.info("Realtime rate limits %s", event.get("rate_limits"))
            return
        if event_type == "input_audio_buffer.committed":
            await self._handle_input_audio_committed(str(event.get("item_id") or ""))
            return
        if event_type == "input_audio_buffer.timeout_triggered":
            await self._handle_idle_timeout(event)
            return
        if event_type == "response.created":
            response = event.get("response")
            metadata = response.get("metadata") if isinstance(response, dict) else None
            metadata_kind = metadata.get("phoneagent_kind") if isinstance(metadata, dict) else None
            state = self._response_state(
                event,
                # All application-created responses carry metadata. Never infer
                # an automatic response's kind from a concurrently queued create.
                kind=str(metadata_kind or "turn"),
            )
            self._creating_kind = None
            self._active_response_key = state.key
            self._generating_response_key = state.key
            self._response_idle.clear()
            logger.info("Realtime response created id=%s kind=%s", state.key, state.kind)
            if self._terminal_override_active and state.kind != "terminal":
                await self._cancel_response_for_terminal_override(state)
            elif state.kind == "terminal":
                self._terminal_override_active = False
            return
        if event_type == "input_audio_buffer.speech_started":
            item_id = str(event.get("item_id") or self._event_id("speech"))
            await self._handle_caller_speech_started(item_id, event)
            return
        if event_type == "input_audio_buffer.speech_stopped":
            await self._handle_caller_speech_stopped(event)
            return
        if event_type == "conversation.item.truncated":
            logger.info(
                "Realtime truncate confirmed item_id=%s audio_end_ms=%s",
                event.get("item_id"),
                event.get("audio_end_ms"),
            )
            return
        if event_type == "conversation.item.input_audio_transcription.completed":
            await self._handle_transcription(event)
            return
        if event_type == "conversation.item.input_audio_transcription.failed":
            await self._handle_transcription_failed(event)
            return
        if event_type == "conversation.item.deleted":
            logger.info("Realtime caller item deleted item_id=%s", event.get("item_id"))
            return
        if event_type in {
            "response.output_item.added",
            "response.output_item.done",
            "response.content_part.added",
            "response.content_part.done",
        }:
            state = self._response_state(event)
            self._capture_output_identity(state, event)
            self._send_pending_truncate(state)
            return
        if event_type == "response.output_audio.delta":
            await self._handle_output_audio_delta(event)
            return
        if event_type == "response.output_audio.done":
            state = self._response_state(event)
            self._capture_output_identity(state, event)
            await self._finish_output_audio(state)
            return
        if event_type == "response.output_audio_transcript.delta":
            state = self._response_state(event)
            self._capture_output_identity(state, event)
            delta = str(event.get("delta", ""))
            if delta:
                state.text += delta
                await self._emit({"type": "transcript_delta", "role": "assistant", "delta": delta})
            return
        if event_type == "response.output_audio_transcript.done":
            state = self._response_state(event)
            self._capture_output_identity(state, event)
            if not state.text:
                state.text = str(event.get("transcript", ""))
            return
        if event_type == "response.done":
            await self._handle_response_done(event)

    async def _handle_response_done(self, event: dict[str, Any]) -> None:
        response = event.get("response")
        state = self._response_state(event)
        self._capture_response_output(state, response)
        if isinstance(response, dict):
            state.response_status = str(response.get("status") or "unknown")
            details = response.get("status_details")
            state.status_details = details if isinstance(details, dict) else None
            usage = response.get("usage")
            if isinstance(usage, dict):
                output_tokens = usage.get("output_tokens")
                if isinstance(output_tokens, int):
                    state.output_tokens = output_tokens
                token_details = usage.get("output_token_details") or usage.get(
                    "output_tokens_details"
                )
                if isinstance(token_details, dict):
                    audio_tokens = token_details.get("audio_tokens")
                    if isinstance(audio_tokens, int):
                        state.audio_output_tokens = audio_tokens
        if self._generating_response_key == state.key:
            self._generating_response_key = None
        if self._generating_response_key is None:
            self._response_idle.set()
        tool_calls = self._collect_tool_calls(response)
        logger.info(
            "Realtime response done id=%s kind=%s status=%s details=%s "
            "output_tokens=%s audio_tokens=%s",
            state.key,
            state.kind,
            state.response_status,
            state.status_details,
            state.output_tokens,
            state.audio_output_tokens,
        )
        await self._emit(
            {
                "type": "realtime_response_status",
                "response_id": state.key,
                "response_kind": state.kind,
                "status": state.response_status,
                "status_details": state.status_details,
                "output_tokens": state.output_tokens,
                "audio_output_tokens": state.audio_output_tokens,
            }
        )
        if state.suppress_transcript:
            state.finalized.set()
            state.playback_closed = True
        else:
            await self._finalize_response(state)
            if (
                state.kind == "terminal"
                and state.response_status == "completed"
                and state.first_audio_at is None
                and not state.audio_done
                and not state.playback_closed
            ):
                await self._response_audio_failed(
                    state, "Realtime final closing completed without audio"
                )
            if (
                state.response_status in {"failed", "incomplete"}
                and not state.audio_done
                and not state.playback_closed
            ):
                await self._response_audio_failed(state, self._response_status_message(state))
        self._dispatch_pending_terminal_response()
        self._dispatch_pending_tool_response()
        if state.kind == "greeting":
            self._request_opening_vad_restore()
        self._dispatch_pending_guarded_turn_response()
        if tool_calls and not self._terminal_override_active:
            may_wait_for_operator = any(
                (self.tool_catalog.get(name) is not None)
                and self.tool_catalog[name].timeout_secs > 10
                for _, name, _ in tool_calls
            )
            if may_wait_for_operator:
                self._spawn(
                    self._run_tool_calls(tool_calls, source_state=state),
                    f"realtime-tool-approval-{state.key}",
                )
            else:
                await self._run_tool_calls(tool_calls, source_state=state)

    @staticmethod
    def _collect_tool_calls(response: Any) -> list[tuple[str, str, str]]:
        """Return (call_id, name, raw_arguments) for every function call."""

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
        source_state: _ResponseState | None = None,
    ) -> None:
        """Execute tools and hand the results back for the spoken answer.

        A response that only calls tools produces no audio, so the caller is
        sitting in silence until the follow-up response is created. Handlers are
        local and synchronous by design; anything slow belongs behind a preamble.
        """

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
            logger.info(
                "Realtime tool call name=%s call_id=%s argument_chars=%d result_chars=%d",
                name,
                call_id,
                len(effective_arguments),
                len(output),
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
            if not self.send_event(
                {
                    "type": "conversation.item.create",
                    "event_id": self._event_id("tool_result"),
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output,
                    },
                }
            ):
                return
            returned += 1
            if name == END_CALL_TOOL_NAME:
                try:
                    result = json.loads(output)
                except json.JSONDecodeError:
                    result = {}
                if isinstance(result, dict) and result.get("accepted") is True:
                    accepted_end_call = result
        if not returned:
            return
        if accepted_end_call is not None:
            await self._accept_ai_end_call(accepted_end_call, source_state=source_state)
            return
        # turn_detection only creates responses from caller speech, so the
        # continuation after a tool result has to be requested explicitly.
        self._pending_tool_response = True
        self._dispatch_pending_tool_response()

    @staticmethod
    def _same_spoken_message(first: str, second: str) -> bool:
        def normalize(value: str) -> str:
            return " ".join(re.sub(r"[^\wÀ-ÿ]+", " ", value.casefold()).split())

        return bool(normalize(first)) and normalize(first) == normalize(second)

    def _disable_turn_automation_for_terminal(self) -> None:
        """Keep a final closing sentence from being cut off by farewell overlap."""

        self.send_event(
            {
                "type": "session.update",
                "event_id": self._event_id("terminal_vad_guard"),
                "session": {
                    "type": "realtime",
                    "audio": {
                        "input": {
                            "turn_detection": self._build_turn_detection(
                                automatic_response=False
                            )
                        }
                    },
                },
            }
        )

    async def _accept_ai_end_call(
        self,
        result: dict[str, Any],
        *,
        source_state: _ResponseState | None,
    ) -> None:
        """Deliver the AI's closing once, then hand one completion to telephony."""

        if self._terminal_completion_notified or self._ai_end_call_requested:
            return
        reason = str(result.get("reason", "conversation complete")).strip()
        closing_message = str(result.get("closing_message", "Goodbye.")).strip()
        self._ai_end_call_requested = True
        self._ai_end_call_reason = reason
        self._pending_tool_response = False
        self._terminal_override_active = True
        self._disable_turn_automation_for_terminal()
        await self._emit(
            {
                "type": "ai_end_call_requested",
                "reason": reason,
                "closing_message": closing_message,
            }
        )
        instruction = (
            "This is the final telephone closing. Speak exactly the quoted sentence once, "
            "with natural warmth, then produce no additional words and ask no question. "
            f"Exact sentence: {json.dumps(closing_message, ensure_ascii=False)}"
        )
        self._terminal_instruction = instruction

        # Realtime normally emits a function call without audio. If a model
        # version already spoke exactly the requested closing in the same
        # response, reuse that playout instead of making the caller hear it twice.
        if (
            source_state is not None
            and source_state.first_audio_at is not None
            and self._same_spoken_message(source_state.text, closing_message)
        ):
            source_state.kind = "terminal"
            self._terminal_override_active = False
            if source_state.playback_closed and source_state.response_status == "completed":
                await self._notify_terminal_completion(
                    f"AI ended call: {self._ai_end_call_reason}"
                )
            elif source_state.audio_end is not None:
                self._start_playback_monitor(source_state)
            return

        self._pending_terminal_instruction = instruction
        self._dispatch_pending_terminal_response()

    def _dispatch_pending_tool_response(self) -> None:
        """Request the spoken tool result only when no response is active.

        A slow autonomous tool can finish after the caller has already started
        another turn.  Sending response.create immediately in that window is
        rejected by Realtime with conversation_already_has_active_response.
        Keep the result in the conversation and dispatch its spoken follow-up
        from response.done instead.
        """

        if not self._pending_tool_response:
            return
        if (
            self._terminal_override_active
            or self._pending_terminal_instruction is not None
            or self._ai_end_call_requested
        ):
            self._pending_tool_response = False
            return
        if self._generating_response_key is not None or self._creating_kind is not None:
            return
        self._pending_tool_response = False
        self._creating_kind = "tool_result"
        self.send_event(
            {
                "type": "response.create",
                "event_id": self._event_id("tool_result"),
                "response": {
                    "output_modalities": ["audio"],
                    "metadata": {"phoneagent_kind": "tool_result"},
                },
            }
        )

    @staticmethod
    def _response_status_message(state: _ResponseState) -> str:
        details = state.status_details or {}
        reason = details.get("reason") or details.get("error") or "unspecified reason"
        return f"Realtime response ended {state.response_status}: {reason}"

    def _response_key(self, event: dict[str, Any]) -> str | None:
        response = event.get("response")
        if isinstance(response, dict) and response.get("id"):
            return str(response["id"])
        if event.get("response_id"):
            return str(event["response_id"])
        return self._active_response_key

    def _response_state(self, event: dict[str, Any], *, kind: str | None = None) -> _ResponseState:
        key = self._response_key(event)
        if key is None:
            self._implicit_response_sequence += 1
            key = f"implicit-{self._implicit_response_sequence}"
        state = self._responses.get(key)
        if state is None:
            state = _ResponseState(
                key=key,
                kind=kind or self._creating_kind or "turn",
            )
            self._responses[key] = state
        return state

    def _capture_output_identity(self, state: _ResponseState, event: dict[str, Any]) -> None:
        item = event.get("item") or event.get("part") or {}
        item_id = event.get("item_id")
        if isinstance(item, dict) and item.get("id"):
            item_id = item["id"]
        if item_id:
            state.output_item_id = str(item_id)
        content_index = event.get("content_index")
        if isinstance(content_index, int) and content_index >= 0:
            state.content_index = content_index

    def _capture_response_output(self, state: _ResponseState, response: Any) -> None:
        if not isinstance(response, dict):
            return
        for item in response.get("output", []) or []:
            if not isinstance(item, dict):
                continue
            if item.get("id") and not state.output_item_id:
                state.output_item_id = str(item["id"])
            for index, content in enumerate(item.get("content", []) or []):
                if not isinstance(content, dict):
                    continue
                transcript = str(content.get("transcript", ""))
                if transcript and not state.text:
                    state.text = transcript
                    state.content_index = index
        self._send_pending_truncate(state)

    async def _handle_output_audio_delta(self, event: dict[str, Any]) -> None:
        state = self._response_state(event)
        self._capture_output_identity(state, event)
        self._send_pending_truncate(state)
        if state.interrupted:
            return
        encoded = event.get("delta")
        if not isinstance(encoded, str) or not encoded:
            return
        try:
            source_pcm = base64.b64decode(encoded, validate=True)
        except ValueError:
            await self._fatal("OpenAI Realtime returned invalid base64 audio")
            return
        if state.first_audio_at is None:
            state.first_audio_at = time.monotonic()
            if self.input_bridge is not None:
                self.input_bridge.set_assistant_playback(True)
            response_ms = (state.first_audio_at - state.created_at) * 1000
            turn_ms = (
                (state.first_audio_at - self._last_speech_stopped_at) * 1000
                if self._last_speech_stopped_at is not None
                else None
            )
            logger.info(
                "Realtime first audio response_id=%s response_latency_ms=%.1f turn_latency_ms=%s",
                state.key,
                response_ms,
                f"{turn_ms:.1f}" if turn_ms is not None else "n/a",
            )
            await self._emit(
                {
                    "type": "realtime_latency",
                    "response_id": state.key,
                    "response_first_audio_ms": round(response_ms, 1),
                    "turn_first_audio_ms": round(turn_ms, 1) if turn_ms is not None else None,
                }
            )
        state.audio_ms_generated += len(source_pcm) / (REALTIME_SAMPLE_RATE * 2) * 1000
        state.pcm_accumulator.extend(
            _resample_frame(state.resampler, source_pcm, REALTIME_SAMPLE_RATE)
        )
        while len(state.pcm_accumulator) >= PHONE_FRAME_BYTES:
            pcm = bytes(state.pcm_accumulator[:PHONE_FRAME_BYTES])
            del state.pcm_accumulator[:PHONE_FRAME_BYTES]
            self._queue_output(_OutputQueueItem(response_key=state.key, pcm=pcm))

    async def _finish_output_audio(self, state: _ResponseState) -> None:
        if state.marker_queued:
            return
        state.audio_done = True
        if state.interrupted:
            state.pcm_accumulator.clear()
            return
        state.pcm_accumulator.extend(_flush_resampler(state.resampler))
        if state.pcm_accumulator:
            remainder = bytes(state.pcm_accumulator)
            state.pcm_accumulator.clear()
            if len(remainder) < PHONE_FRAME_BYTES:
                remainder += b"\x00" * (PHONE_FRAME_BYTES - len(remainder))
            for offset in range(0, len(remainder), PHONE_FRAME_BYTES):
                chunk = remainder[offset : offset + PHONE_FRAME_BYTES]
                if len(chunk) < PHONE_FRAME_BYTES:
                    chunk += b"\x00" * (PHONE_FRAME_BYTES - len(chunk))
                self._queue_output(_OutputQueueItem(state.key, chunk))
        state.marker_queued = True
        self._queue_output(_OutputQueueItem(response_key=state.key))

    def _delivered_frames(self, state: _ResponseState) -> int:
        """Count only this response's own frames that Android actually rendered.

        The session output sequence is global and monotonic for the whole call,
        and an interruption flush discards frames that already consumed sequence
        numbers. Anchoring on the response's first written frame and capping at
        the number of frames written keeps the count honest across every later
        barge-in instead of accumulating the discards.
        """

        if state.first_output_sequence is None:
            return 0
        rendered = self.transport.session.metrics.last_rendered_sequence
        delivered = rendered - state.first_output_sequence + 1
        return max(0, min(delivered, state.frames_written))

    def _delivered_audio_ms(self, state: _ResponseState) -> int:
        """Milliseconds of this response the caller heard, never over-reporting.

        conversation.item.truncate is rejected outright when audio_end_ms
        exceeds the item's real duration ("Audio content of Nms is already
        shorter than Mms"), which would leave the entire un-heard turn in the
        model's conversation. Capping at the audio actually generated keeps the
        truncate applicable even if playout accounting is ever ahead.
        """

        delivered_ms = self._delivered_frames(state) * PHONE_FRAME_MS
        return max(0, min(delivered_ms, int(state.audio_ms_generated)))

    def _assistant_output_active(self) -> bool:
        state = self._responses.get(self._active_response_key or "")
        return bool(
            state
            and not state.playback_closed
            and (state.first_audio_at is not None or not state.finalized.is_set())
        )

    async def _interrupt_for_caller(
        self,
        item_id: str,
        *,
        server_already_interrupted: bool = True,
    ) -> None:
        state = self._responses.get(self._active_response_key or "")
        if state is None or state.interrupted:
            return
        state.interrupted = True
        if not server_already_interrupted and not state.cancel_sent:
            state.cancel_sent = self.send_event(
                {
                    "type": "response.cancel",
                    "event_id": self._event_id("confirmed_opening_barge_in"),
                }
            )
        # Normally turn_detection.interrupt_response already stopped generation
        # server-side. During the opening guard PhoneAgent owns that cancellation.
        delivered_frames = self._delivered_frames(state)
        state.pending_truncate_ms = self._delivered_audio_ms(state)
        self._send_pending_truncate(state)
        self._discard_queued_output()
        state.pcm_accumulator.clear()
        self.transport.output().discard_audio_segment()
        await self.policy.mark_playback_interrupted()
        started = time.monotonic()
        await self.transport.coordinator.interrupt(
            "openai_realtime_caller_barge_in",
            self.transport.output()._flush_phone,
        )
        flush_ms = (time.monotonic() - started) * 1000
        logger.info(
            "Realtime barge-in item_id=%s response_id=%s delivered_ms=%d flush_ms=%.1f",
            item_id,
            state.key,
            state.pending_truncate_ms,
            flush_ms,
        )
        if state.finalized.is_set() and not state.playback_closed:
            await self.policy.playback_stopped(delivered_frames=delivered_frames)
            state.playback_closed = True
        if self._active_response_key == state.key:
            self._active_response_key = None
        if self.input_bridge is not None:
            self.input_bridge.set_assistant_playback(False)

    def _send_pending_truncate(self, state: _ResponseState) -> None:
        if state.truncate_sent or state.pending_truncate_ms is None or state.output_item_id is None:
            return
        # Only record the truncate as sent once it is actually queued, so a
        # reconnect retries it instead of permanently believing it applied.
        state.truncate_sent = self.send_event(
            {
                "type": "conversation.item.truncate",
                "event_id": self._event_id("truncate"),
                "item_id": state.output_item_id,
                "content_index": state.content_index,
                "audio_end_ms": state.pending_truncate_ms,
            }
        )

    def _queue_output(self, item: _OutputQueueItem) -> None:
        """Hand phone audio to the output task without ever blocking the reader.

        Audio deltas are handled on the WebSocket reader task. Waiting here for
        phone playout would hold back every event queued behind them, including
        input_audio_buffer.speech_started, so barge-in latency would grow with
        the amount of speech already buffered.
        """

        self._output_queue.put_nowait(item)
        depth = self._output_queue.qsize()
        if depth >= OUTPUT_QUEUE_WARN_FRAMES and not self._output_queue_warned:
            self._output_queue_warned = True
            logger.warning(
                "Realtime response is buffering %.1fs of speech response_id=%s; "
                "the turn is far longer than a phone conversation should allow",
                depth * PHONE_FRAME_MS / 1000,
                item.response_key,
            )

    def _discard_queued_output(self) -> None:
        self._output_queue_warned = False
        while not self._output_queue.empty():
            try:
                self._output_queue.get_nowait()
                self._output_queue.task_done()
            except asyncio.QueueEmpty:
                break

    def _caller_turn(self, item_id: str) -> _CallerTurnState:
        turn = self._caller_turns.get(item_id)
        if turn is None:
            turn = _CallerTurnState(item_id=item_id)
            self._caller_turns[item_id] = turn
        return turn

    def _request_opening_vad_restore(self) -> None:
        if not self._opening_vad_guard_active:
            return
        if self._opening_guard_items:
            self._opening_vad_restore_pending = True
            return
        if self._set_turn_detection_automation(enabled=True):
            self._opening_vad_guard_active = False
            self._opening_vad_restore_pending = False
            logger.info("Realtime automatic VAD restored after opening guard")

    def _dispatch_pending_guarded_turn_response(self) -> None:
        if not self._pending_guarded_turn_response:
            return
        if self._generating_response_key is not None or self._creating_kind is not None:
            return
        self._pending_guarded_turn_response = False
        self._creating_kind = "turn"
        self.send_event(
            {
                "type": "response.create",
                "event_id": self._event_id("confirmed_opening_turn"),
                "response": {
                    "output_modalities": ["audio"],
                    "metadata": {"phoneagent_kind": "turn"},
                },
            }
        )

    def _schedule_opening_speech_confirmation(self, item_id: str) -> None:
        previous = self._opening_confirmation_tasks.pop(item_id, None)
        if previous is not None and not previous.done():
            previous.cancel()

        async def confirm() -> None:
            await asyncio.sleep(0.08)
            turn = self._caller_turns.get(item_id)
            if turn is None or turn.discarded or not turn.guarded_opening:
                return
            bridge = self.input_bridge
            if bridge is None or not bridge.has_recent_human_speech():
                return
            turn.human_confirmed = True
            logger.info("Confirmed human speech during guarded opening item_id=%s", item_id)
            if self._assistant_output_active():
                await self._interrupt_for_caller(
                    item_id,
                    server_already_interrupted=False,
                )

        task = self._spawn(
            confirm(),
            f"realtime-opening-speech-confirm-{item_id}",
        )
        self._opening_confirmation_tasks[item_id] = task

        def forget(completed: asyncio.Task[Any]) -> None:
            if self._opening_confirmation_tasks.get(item_id) is completed:
                self._opening_confirmation_tasks.pop(item_id, None)

        task.add_done_callback(forget)

    async def _handle_input_audio_committed(self, item_id: str) -> None:
        logger.info("Realtime caller audio committed item_id=%s", item_id)
        if not item_id:
            return
        turn = self._caller_turns.get(item_id)
        if turn is None or not turn.guarded_opening:
            return
        confirmation = self._opening_confirmation_tasks.pop(item_id, None)
        if confirmation is not None and not confirmation.done():
            confirmation.cancel()
        bridge = self.input_bridge
        if not turn.human_confirmed and bridge is not None:
            turn.human_confirmed = bridge.has_recent_human_speech()
        self._opening_guard_items.discard(item_id)
        if turn.human_confirmed:
            self._pending_guarded_turn_response = True
        else:
            await self._discard_caller_turn(turn, "unconfirmed startup carrier audio")
        if self._opening_vad_restore_pending or turn.human_confirmed:
            self._request_opening_vad_restore()
        self._dispatch_pending_guarded_turn_response()

    async def _handle_caller_speech_started(self, item_id: str, event: dict[str, Any]) -> None:
        turn = self._caller_turn(item_id)
        self._idle_reengagements = 0
        logger.info(
            "Realtime caller speech started item_id=%s audio_start_ms=%s assistant_active=%s",
            item_id,
            event.get("audio_start_ms"),
            self._assistant_output_active(),
        )
        active = self._responses.get(self._active_response_key or "")
        if active is not None and active.kind == "terminal":
            logger.info(
                "Ignoring overlap during final AI closing item_id=%s response_id=%s",
                item_id,
                active.key,
            )
            return
        if self._opening_vad_guard_active:
            turn.guarded_opening = True
            self._opening_guard_items.add(item_id)
            self._schedule_opening_speech_confirmation(item_id)
            logger.info(
                "Deferring opening interruption for acoustic confirmation item_id=%s",
                item_id,
            )
            return
        # The server has already stopped generating for this barge-in. Android
        # is still playing what was sent, so the phone must be flushed and the
        # item truncated at the exact point the caller stopped hearing it.
        if self._assistant_output_active():
            await self._interrupt_for_caller(item_id)

    async def _handle_caller_speech_stopped(self, event: dict[str, Any]) -> None:
        self._last_speech_stopped_at = time.monotonic()
        item_id = str(event.get("item_id") or "")
        self._last_speech_item_id = item_id or None
        if not item_id:
            return
        turn = self._caller_turn(item_id)
        turn.stopped = True
        logger.info(
            "Realtime caller speech stopped item_id=%s audio_end_ms=%s",
            item_id,
            event.get("audio_end_ms"),
        )
        if turn.guarded_opening and not turn.human_confirmed:
            bridge = self.input_bridge
            if bridge is not None and bridge.has_recent_human_speech():
                turn.human_confirmed = True
                if self._assistant_output_active():
                    await self._interrupt_for_caller(
                        item_id,
                        server_already_interrupted=False,
                    )

    async def _discard_caller_turn(self, turn: _CallerTurnState, reason: str) -> None:
        if turn.discarded:
            return
        delivered = self.send_event(
            {
                "type": "conversation.item.delete",
                "event_id": self._event_id("discard_input"),
                "item_id": turn.item_id,
            }
        )
        if not delivered:
            # The item stays in the model's conversation. Answering a turn we
            # could not remove is far safer than ignoring the caller entirely.
            logger.warning(
                "Realtime could not delete caller item item_id=%s; keeping the turn",
                turn.item_id,
            )
            return
        turn.discarded = True
        self._discarded_caller_items.add(turn.item_id)
        logger.warning(
            "Realtime suppressed false/ambiguous caller turn item_id=%s reason=%s",
            turn.item_id,
            reason,
        )
        await self._emit(
            {
                "type": "caller_turn_suppressed",
                "item_id": turn.item_id,
                "reason": reason,
            }
        )

    async def _handle_transcription_failed(self, event: dict[str, Any]) -> None:
        # The speech-to-speech model works from the audio itself, so a failed
        # side-channel transcript costs us the transcript line, not the turn.
        logger.warning(
            "Realtime input transcription failed item_id=%s; the model still heard the audio",
            event.get("item_id"),
        )

    @staticmethod
    def _transcription_metadata(event: dict[str, Any]) -> tuple[str | None, float | None]:
        language: str | None = None
        languages = event.get("languages")
        if isinstance(languages, list):
            for candidate in languages:
                code = candidate.get("code") if isinstance(candidate, dict) else candidate
                normalized = str(code or "").strip().lower()
                if normalized:
                    language = normalized.split("-", 1)[0]
                    break
        if language is None and event.get("language"):
            language = str(event["language"]).strip().lower().split("-", 1)[0]
        values: list[float] = []
        for item in event.get("logprobs", []) or []:
            if not isinstance(item, dict):
                continue
            try:
                value = float(item["logprob"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(value):
                values.append(value)
        confidence = math.exp(max(-20.0, min(0.0, sum(values) / len(values)))) if values else None
        return language, confidence

    async def _handle_transcription(self, event: dict[str, Any]) -> None:
        transcript = str(event.get("transcript", "")).strip()
        item_id = str(event.get("item_id", ""))
        if item_id in self._discarded_caller_items:
            logger.info("Ignored transcription for discarded caller item item_id=%s", item_id)
            return
        if item_id and item_id in self._processed_transcriptions:
            return
        turn = self._caller_turns.get(item_id)
        if turn is not None:
            turn.transcript_seen = True
        if not transcript:
            logger.info("Realtime caller transcription completed empty item_id=%s", item_id)
            return
        language, confidence = self._transcription_metadata(event)
        explicit_control = self.policy.is_explicit_conversation_control(transcript)
        trusted = not (
            confidence is not None
            and confidence < LOW_TRANSCRIPTION_CONFIDENCE
            and not explicit_control
        )
        if item_id:
            self._processed_transcriptions.add(item_id)
        # Observation only. The model already answered from the audio; this
        # transcript exists for the operator transcript, caller memory and the
        # task record. It must never be able to suppress a caller turn.
        self._record_turn("user", transcript)
        await self.policy.observe_transcription(
            transcript,
            language_code=language,
            trusted_for_task=trusted,
            transcription_confidence=confidence,
        )
        # The transcript is observation and durable operator evidence. The
        # Realtime model heard the original audio and owns the semantic decision
        # to end the call through the end_call tool; transcription regexes must
        # not make that decision in its place.

    async def _activate_terminal_override(self, kind: str) -> None:
        if self._terminal_completion_notified:
            return
        instruction = self.policy.terminal_response_instruction(kind)
        self._terminal_instruction = instruction
        self._pending_terminal_instruction = instruction
        self._terminal_override_active = True
        self._disable_turn_automation_for_terminal()
        state = self._responses.get(
            self._generating_response_key or self._active_response_key or ""
        )
        logger.info(
            "Realtime terminal control override kind=%s active_response=%s first_audio=%s",
            kind,
            state.key if state is not None else None,
            bool(state and state.first_audio_at is not None),
        )
        if state is not None and state.kind != "terminal":
            await self._cancel_response_for_terminal_override(state)
        if self._generating_response_key is None and self._creating_kind is None:
            self._spawn(
                self._dispatch_terminal_after_event_grace(),
                "realtime-terminal-dispatch",
            )

    async def _cancel_response_for_terminal_override(self, state: _ResponseState) -> None:
        if state.kind == "terminal" or state.suppress_transcript:
            return
        state.suppress_transcript = True
        if self._generating_response_key == state.key and not state.cancel_sent:
            state.cancel_sent = True
            self.send_event(
                {"type": "response.cancel", "event_id": self._event_id("terminal_cancel")}
            )
        if not state.interrupted:
            await self._interrupt_for_caller("terminal-control")

    async def _dispatch_terminal_after_event_grace(self) -> None:
        # Input transcription and response creation are asynchronous. Give an
        # automatic VAD response one event-loop window to surface so it can be
        # cancelled instead of racing the terminal response.
        await asyncio.sleep(0.05)
        if self._generating_response_key is None:
            self._dispatch_pending_terminal_response()

    def _dispatch_pending_terminal_response(self) -> None:
        instruction = self._pending_terminal_instruction
        if (
            instruction is None
            or self._generating_response_key is not None
            or self._creating_kind is not None
            or not self._connected.is_set()
        ):
            return
        self._pending_terminal_instruction = None
        self._terminal_instruction = instruction
        self._terminal_response_attempts += 1
        self._creating_kind = "terminal"
        self.send_event(
            {
                "type": "response.create",
                "event_id": self._event_id("terminal"),
                "response": {
                    "output_modalities": ["audio"],
                    "tool_choice": "none",
                    "metadata": {"phoneagent_kind": "terminal"},
                    "instructions": instruction,
                },
            }
        )

    def _record_turn(
        self, role: str, text: str, *, interrupted: bool = False, interrupted_by: str = "caller"
    ) -> None:
        """Keep a bounded local copy of the conversation for reconnect replay.

        An interrupted turn is generated in full server-side but only partly
        heard. Replaying it verbatim would recreate exactly the divergence the
        truncate exists to prevent, so it is marked as cut off instead.
        """

        cleaned = " ".join(text.split())
        if not cleaned:
            return
        if interrupted:
            cause = (
                "cut off by the caller partway through"
                if interrupted_by == "caller"
                else "cut short partway through when the line dropped"
            )
            cleaned = f"{cleaned} ({cause})"
        self._conversation_log.append((role, cleaned))
        if len(self._conversation_log) > CONVERSATION_REPLAY_TURNS:
            del self._conversation_log[:-CONVERSATION_REPLAY_TURNS]

    def _replay_conversation(self) -> int:
        """Rebuild the conversation on a fresh connection before speaking again.

        A reconnect gives us an empty server-side conversation. Replaying the
        turns keeps the model from re-introducing itself, re-asking answered
        questions, or contradicting what the caller already heard.
        """

        replayed = 0
        for role, text in self._conversation_log:
            content = (
                {"type": "input_text", "text": text}
                if role == "user"
                else {"type": "output_text", "text": text}
            )
            if not self.send_event(
                {
                    "type": "conversation.item.create",
                    "event_id": self._event_id("replay"),
                    "item": {"type": "message", "role": role, "content": [content]},
                }
            ):
                break
            replayed += 1
        if replayed:
            logger.info("Realtime replayed %d conversation turns after reconnect", replayed)
        return replayed

    async def _handle_idle_timeout(self, event: dict[str, Any]) -> None:
        """Re-engage a silent caller instead of waiting on a dead line."""

        item_id = str(event.get("item_id") or "")
        logger.info(
            "Realtime idle timeout item_id=%s audio_start_ms=%s audio_end_ms=%s",
            item_id,
            event.get("audio_start_ms"),
            event.get("audio_end_ms"),
        )
        if item_id:
            # The server commits the silent span as a caller item. It carries no
            # meaning and must not enter the conversation.
            if self.send_event(
                {
                    "type": "conversation.item.delete",
                    "event_id": self._event_id("discard_idle"),
                    "item_id": item_id,
                }
            ):
                self._discarded_caller_items.add(item_id)
        await self._emit({"type": "caller_idle", "item_id": item_id})
        if (
            self._terminal_override_active
            or self._pending_terminal_instruction is not None
            or self._terminal_completion_notified
            or self._generating_response_key is not None
            or self._creating_kind is not None
            or self._assistant_output_active()
        ):
            return
        if self._idle_reengagements >= MAX_IDLE_REENGAGEMENTS:
            logger.info("Realtime idle re-engagement limit reached; staying quiet")
            return
        self._idle_reengagements += 1
        self._creating_kind = "reengage"
        self.send_event(
            {
                "type": "response.create",
                "event_id": self._event_id("reengage"),
                "response": {
                    "output_modalities": ["audio"],
                    "metadata": {"phoneagent_kind": "reengage"},
                    "instructions": (
                        "The caller has been silent for a while. In their current "
                        "language, say one short, warm line to check they are still "
                        "there, then wait. Do not repeat the introduction, the "
                        "company pitch, or any question you already asked."
                    ),
                },
            }
        )

    async def _finalize_response(self, state: _ResponseState) -> None:
        if state.finalized.is_set():
            return
        text = state.text.strip()
        if text:
            self._record_turn(
                "assistant",
                text,
                interrupted=state.interrupted,
                interrupted_by=state.interrupted_by,
            )
            await self.policy.finalize_response(
                text,
                response_kind=state.kind,
                enforce_spoken_policy=False,
            )
            await self.policy.playback_started()
            if state.interrupted:
                await self.policy.mark_playback_interrupted()
                await self.policy.playback_stopped(delivered_frames=self._delivered_frames(state))
                state.playback_closed = True
        state.finalized.set()
        if state.audio_end is not None and not state.playback_closed:
            self._start_playback_monitor(state)

    def _start_playback_monitor(self, state: _ResponseState) -> None:
        if state.monitor_started or state.playback_closed or state.audio_end is None:
            return
        state.monitor_started = True
        self._spawn(self._monitor_playback(state), f"realtime-playback-{state.key}")

    async def _monitor_playback(self, state: _ResponseState) -> None:
        try:
            await asyncio.wait_for(state.finalized.wait(), timeout=3.0)
            if state.playback_closed or state.audio_end is None:
                return
            generation_id, end_sequence = state.audio_end
            deadline = time.monotonic() + PLAYBACK_ACK_TIMEOUT_SECS
            while self._running and time.monotonic() < deadline:
                rendered = self.transport.session.metrics.last_rendered_sequence
                if self.transport.session.generation_id != generation_id:
                    await self.policy.mark_playback_interrupted()
                    await self.policy.playback_stopped(
                        delivered_frames=self._delivered_frames(state)
                    )
                    state.playback_closed = True
                    if self.input_bridge is not None:
                        self.input_bridge.set_assistant_playback(False)
                    if self._active_response_key == state.key:
                        self._active_response_key = None
                    return
                if rendered >= end_sequence:
                    if state.response_status == "completed":
                        await self.policy.playback_stopped(
                            delivered_frames=max(1, self._delivered_frames(state))
                        )
                        state.playback_closed = True
                        if self.input_bridge is not None:
                            self.input_bridge.set_assistant_playback(False)
                        if state.kind == "terminal":
                            reason = (
                                f"AI ended call: {self._ai_end_call_reason}"
                                if self._ai_end_call_reason
                                else "terminal response delivered"
                            )
                            await self._notify_terminal_completion(reason)
                    elif state.response_status == "cancelled" and state.interrupted:
                        await self.policy.mark_playback_interrupted()
                        await self.policy.playback_stopped(
                            delivered_frames=self._delivered_frames(state)
                        )
                        state.playback_closed = True
                        if self.input_bridge is not None:
                            self.input_bridge.set_assistant_playback(False)
                    else:
                        await self._response_audio_failed(
                            state, self._response_status_message(state)
                        )
                    if self._active_response_key == state.key:
                        self._active_response_key = None
                    return
                await asyncio.sleep(0.01)
            if not state.playback_closed:
                await self._response_audio_failed(
                    state, "Android did not acknowledge rendering this Realtime response"
                )
        except TimeoutError:
            # response.done never arrived. Close the turn as failed rather than
            # letting this task die and strand the response as un-interruptible.
            await self._response_audio_failed(state, "Realtime response never reported completion")
        except asyncio.CancelledError:
            raise

    async def _response_audio_failed(self, state: _ResponseState, message: str) -> None:
        if state.playback_closed:
            return
        if not state.finalized.is_set():
            try:
                await asyncio.wait_for(state.finalized.wait(), timeout=3.0)
            except TimeoutError:
                pass
        await self.policy.playback_failed(message)
        state.playback_closed = True
        if self.input_bridge is not None:
            self.input_bridge.set_assistant_playback(False)
        if self._active_response_key == state.key:
            self._active_response_key = None
        if state.kind != "terminal" or self._terminal_completion_notified:
            return
        if (
            self._terminal_instruction
            and self._terminal_response_attempts < MAX_TERMINAL_RESPONSE_ATTEMPTS
            and self._running
        ):
            logger.warning(
                "Retrying final closing after audio failure attempt=%d error=%s",
                self._terminal_response_attempts,
                message,
            )
            self._pending_terminal_instruction = self._terminal_instruction
            self._terminal_override_active = True
            self._dispatch_pending_terminal_response()
            return
        await self._notify_terminal_completion(
            f"AI ended call after closing audio failure: {message}"
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

    async def _fatal(self, message: str, *, terminal: bool = False) -> None:
        logger.error("%s", message)
        if not self._session_updated.is_set():
            self._startup_error = RuntimeError(message)
            self._session_updated.set()
        await self._emit({"type": "call_error", "message": message})
        if terminal and not self._terminal_failure_notified:
            self._terminal_failure_notified = True
            sink = self.terminal_failure_sink
            if sink is not None:
                result = sink(message)
                if inspect.isawaitable(result):
                    await result

    def add_external_context(self, text: str, *, respond: bool = True) -> None:
        self._record_turn("user", text)
        self.send_event(
            {
                "type": "conversation.item.create",
                "event_id": self._event_id("text"),
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        if respond:
            self._creating_kind = "turn"
            self.send_event(
                {
                    "type": "response.create",
                    "event_id": self._event_id("response"),
                    "response": {"metadata": {"phoneagent_kind": "turn"}},
                }
            )

    def send_text_message(self, text: str) -> None:
        self.add_external_context(text, respond=True)

    async def greet(self) -> None:
        async with self._greet_lock:
            if self._greeted:
                return
            self._greeted = True
            language = self.config.providers.stt_language.lower()
            openings = self.policy.task_contract.get("opening_greeting", {})
            key = "fr" if language.startswith("fr") else "en"
            compiler = self.policy.persona_compiler
            identity = getattr(
                compiler,
                "effective_identity",
                compiler.persona_data.get("identity", {}),
            )
            greeting = self.policy.call_context.opening_greeting(
                name=str(identity.get("name", "Adam")),
                role=str(identity.get("role", "")),
                language=language,
                configured_outbound=str(openings.get(key, "")),
            )
            # This is true when the opening is dispatched, not only when its
            # asynchronous transcript completes. It closes the immediate-refusal
            # race when the caller barges into the greeting.
            self.policy.note_opening_attempted()
            self._opening_guard_items.clear()
            self._opening_vad_restore_pending = False
            self._opening_vad_guard_active = self._set_turn_detection_automation(
                enabled=False
            )
            if self._opening_vad_guard_active:
                logger.info("Realtime opening VAD guard enabled")
            self.send_event(
                {
                    "type": "session.update",
                    "event_id": self._event_id("opening_state"),
                    "session": {
                        "type": "realtime",
                        "instructions": self._session_instructions(),
                    },
                }
            )
            self._creating_kind = "greeting"
            self.send_event(
                {
                    "type": "response.create",
                    "event_id": self._event_id("greeting"),
                    "response": {
                        "output_modalities": ["audio"],
                        "metadata": {"phoneagent_kind": "greeting"},
                        "instructions": (
                            "The call has just connected. Say exactly this opening once, "
                            f"naturally and completely, then wait: {greeting}"
                        ),
                    },
                }
            )
            if self.input_bridge is not None:
                self.input_bridge.enable()

    async def stop(self, timeout_secs: float = 10.0) -> None:
        del timeout_secs
        await self.close()

    async def cancel(self, reason: str) -> None:
        logger.info("cancelling OpenAI Realtime WebSocket reason=%s", reason)
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._running = False
        if self.input_bridge is not None:
            quality = self.input_bridge.quality_snapshot()
            logger.info("Realtime caller audio quality summary %s", quality)
            await self._emit({"type": "caller_audio_quality", **quality})
            self.transport.remove_audio_listener(self.input_bridge.push_pcm_frame)
            self.transport.remove_output_audio_listener(self.input_bridge.note_output_pcm)
            self.input_bridge.stop()
        if self.ws is not None:
            await self.ws.close()
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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
