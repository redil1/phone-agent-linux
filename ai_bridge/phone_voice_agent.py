#!/usr/bin/env python3
"""Production per-call host for the rooted Android cellular AI gateway."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger as pipecat_logger

from phone_agent_gateway.mac_client.framed_link import LinkError, LinkPorts
from phone_agent_gateway.mac_client.gateway_client import CallState, CallStatus
from phone_agent_gateway.mac_client.protocol_client import AuthenticatedPhoneAgentClient

from .call_recording import CallRecordingSession, RecordingConfig, enforce_recording_retention
from .pipecat_transport import PhoneAgentTransport, PhoneAgentTransportParams
from .production_pipeline import (
    ProductionCallPipeline,
    ProviderServices,
    create_provider_services,
    prewarm_primary_llm,
    prewarm_speech_models,
)
from .runtime_config import RuntimeConfig
from .session import CallSessionState, SessionPhase
from .voice_host_lock import VoiceHostBusyError, VoiceHostLock

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("PhoneVoiceAgent")


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class ActiveCallRuntime:
    session: CallSessionState
    client: AuthenticatedPhoneAgentClient
    transport: PhoneAgentTransport
    pipeline: Any | None = None
    media_attached: bool = False
    phone_audio_route: dict[str, Any] = field(default_factory=dict)
    recorder: CallRecordingSession | None = None


class PhoneVoiceAgent:
    """Own one authenticated gateway link and one Pipecat worker per call."""

    def __init__(self, config: RuntimeConfig, *, dial_number: str | None = None) -> None:
        if config.link_authentication_key is None and config.call_channel != "whatsapp":
            raise ValueError("PHONE_AGENT_LINK_KEY_FILE or PHONE_AGENT_LINK_KEY_BASE64 is required")
        self.config = config
        self._dial_number = dial_number
        self._outbound_number = dial_number
        # A commanded host serves call after call, so it must not stop when one
        # of them ends. Only the one-shot CLI form exits after its own call.
        self._command_stdin = _env_flag("PHONE_AGENT_COMMAND_STDIN")
        self._one_call_mode = dial_number is not None and not self._command_stdin
        self.call_direction = "outbound" if dial_number is not None else "inbound"
        self._call_context_emitted = False
        self._outbound_seen_live_state = False
        self._runtime: ActiveCallRuntime | None = None
        self._stopping = asyncio.Event()
        self._last_state: CallState | None = None
        self._prepared_services: ProviderServices | None = None
        self._speech_models_prewarmed = False
        self._enforcer_task: asyncio.Task[None] | None = None
        self._greeting_attempted = False
        self._auto_answer_attempted = False
        self._active_caller_id = ""

    async def run(self) -> None:
        with VoiceHostLock(self.config.voice_lock_path):
            logger.info("starting authenticated PhoneAgent voice host pid=%d", os.getpid())
            await self._prewarm_primary_llm()
            await self._prepare_provider_services()
            self._emit_voice_host_ready()
            await self._replace_runtime(retry=True)
            if self._dial_number:
                logger.info("placing outbound call to %s", self._dial_number)
                runtime = self._require_runtime()
                dial_number = self._dial_number
                resp = await self._place_outbound_call(runtime, dial_number)
                if resp.get("status") != "ok":
                    logger.error("dial failed: %s", resp)
                    self._stopping.set()
                self._dial_number = None
            command_task: asyncio.Task[None] | None = None
            if self._command_stdin:
                command_task = asyncio.create_task(
                    self.serve_commands(), name="voice-host-commands"
                )
            try:
                while not self._stopping.is_set():
                    runtime = self._require_runtime()
                    try:
                        status = await asyncio.to_thread(runtime.client.get_status)
                        await self._handle_status(status)
                    except LinkError as exc:
                        logger.warning("phone link status poll warning: %s", exc)
                        if not runtime.client.link.media_connected:
                            await self._recover_runtime_link(runtime)
                    if not self._stopping.is_set():
                        poll_interval = 0.5 if runtime.session.is_active else 0.25
                        await asyncio.sleep(poll_interval)
            finally:
                if command_task is not None:
                    command_task.cancel()
                    await asyncio.gather(command_task, return_exceptions=True)
                await self._close_runtime(hangup=True)

    async def _prewarm_primary_llm(self) -> None:
        """Reach model-ready state before monitoring or attaching cellular calls."""
        # Pipeline mode is always cascade in production

        delay = 0.5
        while not self._stopping.is_set():
            try:
                prewarm_ms = await prewarm_primary_llm(self.config.providers)
                if prewarm_ms is not None:
                    logger.info(
                        "primary Ollama model resident model=%s prewarm_ms=%.1f",
                        self.config.providers.llm_model,
                        prewarm_ms,
                    )
                return
            except Exception as exc:
                logger.warning("primary LLM prewarm failed; retrying in %.2fs: %s", delay, exc)
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, 5.0)

    async def _place_outbound_call(
        self,
        runtime: ActiveCallRuntime,
        dial_number: str,
    ) -> dict[str, Any]:
        """Place an outbound call via the authenticated client."""

        return await asyncio.to_thread(runtime.client.dial, dial_number)

    async def _handle_status(self, status: CallStatus) -> None:
        if status.state is not self._last_state:
            logger.info("cellular state=%s", status.state.value)
            self._last_state = status.state
            # A warm host outlives its calls, so its exit can no longer be what
            # tells Studio a call finished. Publish the transition instead.
            self._emit_event({"type": "call_state", "state": status.state.value})
        if status.state is not CallState.RINGING:
            self._auto_answer_attempted = False
        if status.state in {CallState.IDLE, CallState.DISCONNECTED}:
            self._call_context_emitted = False
        if not self._call_context_emitted and status.state in {
            CallState.RINGING,
            CallState.DIALING,
            CallState.CONNECTING,
            CallState.ACTIVE,
        }:
            self._call_context_emitted = True
            self._emit_event(
                {
                    "type": "call_context",
                    "direction": self.call_direction,
                    "mode": (
                        "cold_prospecting"
                        if self.call_direction == "outbound"
                        else "intent_led"
                    ),
                    "phase": (
                        "await_permission"
                        if self.call_direction == "outbound"
                        else "intent_discovery"
                    ),
                    "interest": (
                        "unknown"
                        if self.call_direction == "outbound"
                        else "caller_initiated"
                    ),
                    "product_qualification_unlocked": self.call_direction == "inbound",
                }
            )

        runtime = self._require_runtime()
        if status.state in {
            CallState.DIALING,
            CallState.CONNECTING,
            CallState.ACTIVE,
            CallState.HOLDING,
        }:
            self._outbound_seen_live_state = True
        if status.state is CallState.RINGING:
            if self.config.auto_answer:
                if self._auto_answer_attempted:
                    return
                self._auto_answer_attempted = True
                logger.info("answering incoming call under configured auto-answer policy")
                try:
                    await asyncio.to_thread(runtime.client.answer)
                except LinkError as exc:
                    if "No ringing call" in str(exc):
                        logger.info(
                            "incoming answer raced a call-state transition; refreshing state"
                        )
                        return
                    self._auto_answer_attempted = False
                    raise
                return

        should_start_active_call = runtime.pipeline is None
        if status.state is CallState.ACTIVE and should_start_active_call:
            await self._start_call(runtime, status)
            return

        if (
            self._one_call_mode
            and self._outbound_seen_live_state
            and status.state in {CallState.IDLE, CallState.DISCONNECTED}
        ):
            logger.info("outbound call finished; stopping one-call voice host")
            self._stopping.set()
            return

        if status.state in {CallState.IDLE, CallState.DISCONNECTED} and runtime.pipeline:
            await self._replace_runtime()
            self._outbound_number = None
            self._greeting_attempted = False
            await self._prepare_provider_services()

    async def _hardware_silence_enforcer_loop(self, session: CallSessionState) -> None:
        """Keep only the physical microphone path muted during an active call."""
        cmd = (
            "su -c 'tinymix -D 0 143 1; "
            "tinymix -D 0 159 0; tinymix -D 0 160 0; "
            "cmd media_session volume --stream 0 --set 7'"
        )
        while not self._stopping.is_set() and session.is_active:
            try:
                import subprocess

                adb_command = ["adb"]
                if self.config.device_id:
                    adb_command.extend(["-s", self.config.device_id])
                adb_command.extend(["shell", cmd])
                await asyncio.to_thread(
                    subprocess.run,
                    adb_command,
                    capture_output=True,
                    timeout=2.0,
                )
            except Exception:
                pass
            await asyncio.sleep(1.0)

    async def _require_live_injection_route(
        self, runtime: ActiveCallRuntime, timeout_secs: float = 5.0
    ) -> None:
        """Wait for Android to establish the Telephony TX route and confirm live media."""
        start_time = asyncio.get_running_loop().time()
        last_error_msg = ""
        attempted_recovery = False
        while asyncio.get_running_loop().time() - start_time < timeout_secs:
            report = await asyncio.to_thread(runtime.client.get_audio_status)
            audio = report.get("audio", report)
            route = str(audio.get("injection_route", "not_started"))
            capture_source = str(audio.get("capture_source", "not_started"))
            capture_ready = bool(audio.get("rx_connected")) and capture_source not in {
                "",
                "not_started",
            }
            if audio.get("tx_connected") and route not in {"", "not_started"} and capture_ready:
                runtime.phone_audio_route = {
                    "capture_source": capture_source,
                    "capture_proof": str(audio.get("capture_proof", "unknown")),
                    "injection_route": route,
                    "injection_proof": str(audio.get("injection_proof", "unknown")),
                    "network_format": str(audio.get("network_format", "unknown")),
                }
                if capture_source != "VOICE_DOWNLINK":
                    logger.warning(
                        "Caller capture is using fallback source=%s; speech quality or echo "
                        "may be degraded",
                        capture_source,
                    )
                logger.info(
                    "telephony media routes live capture_source=%s injection_route=%s "
                    "network_format=%s",
                    capture_source,
                    route,
                    runtime.phone_audio_route["network_format"],
                )
                self._emit_event({"type": "phone_audio_route", **runtime.phone_audio_route})
                return

            last_err = str(audio.get("last_error", ""))
            if "saturated by orphaned tracks" in last_err and not attempted_recovery:
                logger.warning(
                    "Detected saturated Android audioserver; attempting hardware recovery..."
                )
                attempted_recovery = True
                try:
                    import subprocess

                    cmd = ["adb"]
                    if self.config.device_id:
                        cmd.extend(["-s", self.config.device_id])
                    cmd.extend(["shell", "su -c 'pkill -f audioserver || killall audioserver'"])
                    await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=2.0)
                except Exception as exc:
                    logger.warning("Could not execute audioserver recovery: %s", exc)

            last_error_msg = (
                f"rx_connected={audio.get('rx_connected')!r}, "
                f"capture_source={capture_source!r}, tx_connected={audio.get('tx_connected')!r}, "
                f"injection_route={route!r}, "
                f"last_error={last_err!r}"
            )
            await asyncio.sleep(0.1)

        raise RuntimeError(
            f"Android did not start the Telephony TX route in {timeout_secs}s ({last_error_msg})"
        )

    async def _start_call(self, runtime: ActiveCallRuntime, status: CallStatus) -> None:
        logger.info("attaching authenticated full-duplex media")
        # Allow 400ms for cellular baseband DSP routing to stabilize upon ACTIVE transition
        await asyncio.sleep(0.4)

        try:
            # One call gets one physical Telephony-TX route attempt. The MTK
            # audio policy can orphan a restored INCALL_MUSIC track after a
            # failed setPreferredDevice(), so retrying here consumes additional
            # native mixer slots without making the route more likely to work.
            await asyncio.to_thread(runtime.client.connect_media)
            if self.config.call_channel != "whatsapp":
                await self._require_live_injection_route(runtime)
        except Exception as exc:
            android_error = ""
            try:
                report = await asyncio.to_thread(runtime.client.get_audio_status)
                audio = report.get("audio", report)
                android_error = str(audio.get("last_error", "")).strip()
            except Exception:
                pass
            detail = f"{exc}"
            if android_error:
                detail = f"{detail}; Android: {android_error}"
            message = f"Phone uplink unavailable; the caller would hear silence: {detail}"
            logger.error("%s", message)
            self._emit_event({"type": "call_error", "message": message})
            try:
                await asyncio.to_thread(runtime.client.hangup)
            except Exception:
                logger.warning("could not hang up after uplink failure", exc_info=True)
            if self._one_call_mode:
                self._stopping.set()
            else:
                await self._replace_runtime()
            return
        runtime.media_attached = True
        runtime.session.set_phase(SessionPhase.ACTIVE)
        if self.config.call_channel != "whatsapp":
            self._enforcer_task = asyncio.create_task(
                self._hardware_silence_enforcer_loop(runtime.session),
                name="hardware_silence_enforcer",
            )
        services = self._prepared_services
        self._prepared_services = None
        caller_id = (
            status.incoming_number or self._outbound_number or f"unknown:{runtime.session.call_id}"
        )
        self._active_caller_id = caller_id
        try:
            recording_config = RecordingConfig.from_env()
            if recording_config.enabled and recording_config.consent_granted:
                await asyncio.to_thread(enforce_recording_retention, recording_config)
            runtime.recorder = CallRecordingSession.create_if_authorized(
                call_id=str(runtime.session.call_id),
                caller_id=caller_id,
                channel=self.config.call_channel,
                sample_rate=self.config.sample_rate,
                config=recording_config,
            )
            if runtime.recorder is not None:
                runtime.transport.add_audio_listener(runtime.recorder.record_remote)
                runtime.transport.add_output_audio_listener(runtime.recorder.record_agent)
                self._emit_event(
                    {
                        "type": "recording_started",
                        "recording_id": runtime.recorder.recording_id,
                    }
                )
        except Exception as exc:
            # Recording is an observer. Its failure is visible but can never
            # interrupt the live media path or alter either call provider.
            logger.error("call recording could not start: %s", exc)
            self._emit_event(
                {"type": "recording_error", "message": "Call recording could not start"}
            )
        pipeline = ProductionCallPipeline(
            runtime.transport,
            self.config,
            services=services,
            caller_id=caller_id,
            call_direction=self.call_direction,
            event_sink=self._emit_event,
            call_completion_sink=self._call_completion_sink(runtime),
        )
        runtime.pipeline = pipeline
        try:
            await pipeline.start()
            await self._greet_pipeline_once(pipeline)
        except Exception as exc:
            logger.exception("voice pipeline could not start: %s", exc)
            self._emit_event(
                {"type": "call_error", "message": f"Voice pipeline failed to start: {exc}"}
            )
            await pipeline.cancel("pipeline startup failed")
            runtime.pipeline = None
            raise

    def _call_completion_sink(self, runtime: ActiveCallRuntime):
        """Build the single guarded telephony completion path for every pipeline."""

        async def complete_call(reason: str) -> None:
            if self._runtime is not runtime:
                return
            logger.info("ending phone call after terminal agent turn: %s", reason)
            try:
                await asyncio.to_thread(runtime.client.hangup)
            except Exception:
                logger.warning("could not hang up after terminal agent turn", exc_info=True)
            if self._one_call_mode:
                self._stopping.set()

        return complete_call

    async def _greet_pipeline_once(self, pipeline: Any) -> None:
        """Attempt the opening once per cellular call, including across media recovery."""

        if self._greeting_attempted:
            logger.info(
                "opening greeting already attempted for this call; continuing without replay"
            )
            return
        # Fail closed before awaiting live TTS: neither a synthesis failure nor
        # a media disconnect may restart the script and repeat the opening.
        self._greeting_attempted = True
        await pipeline.greet()

    async def _recover_runtime_link(self, runtime: ActiveCallRuntime) -> None:
        """Reconnect media in place so the pipeline, context, and call stage survive."""

        delay = 0.25
        attempts = 0
        # Re-opening media creates another physical Telephony-TX AudioTrack on
        # Android. One bounded recovery attempt is safe; a retry loop can fill
        # the vendor INCALL_MUSIC mixer with orphaned restored tracks.
        max_attempts = 1
        while not self._stopping.is_set() and self._runtime is runtime:
            attempts += 1
            try:
                await asyncio.to_thread(runtime.client.reconnect)
                logger.info(
                    "recovered authenticated phone link in place call_id=%s epoch=%s",
                    runtime.session.call_id,
                    runtime.session.link_epoch,
                )
                return
            except Exception as exc:
                err_str = str(exc)
                logger.warning("phone media recovery attempt %d/%d failed: %s", attempts, max_attempts, exc)

                try:
                    status = await asyncio.to_thread(runtime.client.get_status)
                    if status.state in {CallState.IDLE, CallState.DISCONNECTED}:
                        logger.info("phone call is %s; ending media recovery", status.state.value)
                        await self._replace_runtime()
                        return
                except Exception:
                    pass

                if (
                    attempts >= max_attempts
                    or "Telecom reports IDLE" in err_str
                    or "requires an ACTIVE call" in err_str
                ):
                    logger.info("phone media recovery stopping after %d attempts; resetting runtime", attempts)
                    if runtime.session.is_active:
                        try:
                            await asyncio.to_thread(runtime.client.hangup)
                        except Exception:
                            logger.warning(
                                "could not hang up after terminal media recovery failure",
                                exc_info=True,
                            )
                    if self._one_call_mode:
                        self._stopping.set()
                    else:
                        await self._replace_runtime()
                    return

                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, 5.0)

    async def _prepare_provider_services(self) -> None:
        """Construct heavyweight provider objects before a call becomes active."""

        if self._prepared_services is not None or self._stopping.is_set():
            return
        if not self._speech_models_prewarmed:
            timings = await prewarm_speech_models(self.config.providers)
            self._speech_models_prewarmed = True
            for model, elapsed_ms in timings.items():
                logger.info("speech model prewarmed model=%s elapsed_ms=%.1f", model, elapsed_ms)
        started = time.perf_counter()
        self._prepared_services = await asyncio.to_thread(
            create_provider_services,
            self.config.providers,
            self.config.sample_rate,
        )
        logger.info(
            "speech providers ready stt=%s tts=%s elapsed_ms=%.1f",
            self.config.providers.stt_provider,
            self.config.providers.tts_provider,
            (time.perf_counter() - started) * 1000,
        )

    def _emit_voice_host_ready(self) -> None:
        """Publish the effective configuration after every selected pipeline is warm."""

        # The Studio keeps this process resident between calls. Publish the
        # parsed configuration, rather than asking the parent to trust that the
        # environment it supplied was actually applied.
        providers = self.config.providers
        self._emit_event(
            {
                "type": "voice_host_ready",
                "config": {
                    "pipeline_mode": providers.pipeline_mode,
                    "stt_provider": providers.stt_provider,
                    "stt_model": providers.stt_model,
                    "stt_language": providers.stt_language,
                    "llm_provider": providers.llm_provider,
                    "llm_model": providers.llm_model,
                    "tts_provider": providers.tts_provider,
                    "tts_model": providers.tts_model,
                    "tts_voice_id": providers.tts_voice_id,
                    "tts_aggregation": providers.tts_aggregation,
                    "task_id": self.config.task_id,
                    "system_prompt_sha256": hashlib.sha256(
                        self.config.system_prompt.encode("utf-8")
                    ).hexdigest(),
                    "auto_answer": self.config.auto_answer,
                },
            }
        )

    async def _replace_runtime(self, *, retry: bool = False) -> None:
        await self._close_runtime(hangup=False)
        delay = 0.25
        while not self._stopping.is_set():
            runtime = self._new_runtime()
            try:
                await asyncio.to_thread(runtime.client.connect_control)
                self._runtime = runtime
                self._last_state = None
                logger.info(
                    "gateway control ready call_id=%s epoch=%s",
                    runtime.session.call_id,
                    runtime.session.link_epoch,
                )
                return
            except Exception as exc:
                runtime.client.close()
                if not retry:
                    raise
                logger.warning("gateway unavailable; retrying in %.2fs: %s", delay, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)

    def _new_runtime(self) -> ActiveCallRuntime:
        if self.config.call_channel == "whatsapp":
            session = CallSessionState(uuid4_str=False)
            transport = PhoneAgentTransport(
                PhoneAgentTransportParams(audio_out_sample_rate=self.config.sample_rate),
                session,
            )
            return self._new_whatsapp_runtime(session, transport)

        session = CallSessionState()
        transport = PhoneAgentTransport(
            PhoneAgentTransportParams(audio_out_sample_rate=self.config.sample_rate),
            session,
        )
        client = AuthenticatedPhoneAgentClient(
            session,
            self.config.link_authentication_key or b"",
            host=self.config.control_host,
            ports=LinkPorts(
                legacy_http=self.config.control_port,
                downlink=self.config.rx_port,
                uplink=self.config.tx_port,
                control=self.config.protocol_control_port,
            ),
            device_id=self.config.device_id,
            auto_forward_adb=self.config.use_adb_forward,
        )
        client.link.on_audio_received(transport.feed_phone_frame)
        transport.set_tx_handler(client.link.send_audio_chunk)
        transport.set_audio_end_handler(client.link.send_audio_end_marker)
        transport.set_flush_handler(client.flush_audio)

        if self.config.call_channel == "whatsapp_phone":
            # Same link, same audio path, same everything below the dial. Only
            # how the call is started changes, so the cellular route above is
            # reused rather than reimplemented.
            from .whatsapp_phone_client import WhatsAppPhoneClient

            client = WhatsAppPhoneClient(
                client,
                device_id=self.config.device_id,
                country_code=self.config.whatsapp_country_code,
            )
            logger.info("call channel: WhatsApp placed by the phone, cellular audio path")
        return ActiveCallRuntime(session=session, client=client, transport=transport)

    def _new_whatsapp_runtime(
        self, session: CallSessionState, transport: PhoneAgentTransport
    ) -> ActiveCallRuntime:
        """The same four bindings over WhatsApp instead of the cellular link.

        Deliberately a separate branch rather than a parameterised one: the
        cellular construction above stays exactly as it was, so selecting this
        channel cannot alter how a GSM call is built.
        """

        from .whatsapp_client import WhatsAppPhoneClient

        client = WhatsAppPhoneClient(
            session,
            country_code=self.config.whatsapp_country_code,
            max_duration_secs=self.config.whatsapp_max_duration_secs,
        )
        client.link.on_audio_received(transport.feed_audio_bytes)
        transport.set_tx_handler(client.link.send_audio_chunk)
        transport.set_audio_end_handler(client.link.send_audio_end_marker)
        transport.set_flush_handler(client.flush_audio)
        logger.info("call channel: WhatsApp (the cellular link is not used)")
        return ActiveCallRuntime(session=session, client=client, transport=transport)

    async def _close_runtime(self, *, hangup: bool) -> None:
        runtime = self._runtime
        self._runtime = None
        if runtime is None:
            return
        if hangup:
            try:
                status = await asyncio.to_thread(runtime.client.get_status)
                if status.state in {
                    CallState.RINGING,
                    CallState.DIALING,
                    CallState.CONNECTING,
                    CallState.ACTIVE,
                    CallState.HOLDING,
                }:
                    await asyncio.to_thread(runtime.client.hangup)
            except Exception:
                logger.warning("could not hang up during shutdown", exc_info=True)
        if runtime.pipeline is not None:
            try:
                await runtime.pipeline.stop()
            except Exception:
                logger.warning("pipeline cleanup failed", exc_info=True)
        recorder = runtime.recorder
        runtime.recorder = None
        if recorder is not None:
            runtime.transport.remove_audio_listener(recorder.record_remote)
            runtime.transport.remove_output_audio_listener(recorder.record_agent)
            try:
                recording = await asyncio.to_thread(recorder.finalize, outcome="call_ended")
                self._emit_event(
                    {
                        "type": "recording_finalized",
                        "recording_id": recording.recording_id,
                        "complete": recording.complete,
                        "dropped_frames": recording.dropped_frames,
                    }
                )
            except Exception:
                logger.warning("call recording cleanup failed", exc_info=True)
                self._emit_event(
                    {"type": "recording_error", "message": "Call recording could not finalize"}
                )
        snapshot = runtime.session.snapshot()
        metrics = snapshot.metrics
        logger.info(
            "call audio summary input_frames=%s dropped_input=%s sequence_gaps=%s "
            "stale_input=%s output_frames=%s dropped_output=%s capture_source=%s",
            metrics["input_frames"],
            metrics["dropped_input_frames"],
            metrics["sequence_gaps"],
            metrics["stale_input_frames"],
            metrics["output_frames"],
            metrics["dropped_output_frames"],
            runtime.phone_audio_route.get("capture_source", "unknown"),
        )
        self._emit_event({"type": "audio_quality", **metrics, **runtime.phone_audio_route})
        enforcer = self._enforcer_task
        self._enforcer_task = None
        if enforcer is not None and not enforcer.done():
            enforcer.cancel()
            await asyncio.gather(enforcer, return_exceptions=True)
        phase = runtime.session.snapshot().phase
        if phase in {SessionPhase.ACTIVE, SessionPhase.CONNECTING}:
            runtime.session.set_phase(SessionPhase.ENDING)
        if runtime.session.snapshot().phase is SessionPhase.ENDING:
            runtime.session.set_phase(SessionPhase.CLOSED)
        await asyncio.to_thread(runtime.client.close)

    def _require_runtime(self) -> ActiveCallRuntime:
        if self._runtime is None:
            raise RuntimeError("phone runtime is not connected")
        return self._runtime

    def request_stop(self) -> None:
        self._stopping.set()

    async def serve_commands(self) -> None:
        """Accept dial requests on stdin so one warm host serves many calls.

        Spawning a host per outbound call reloads every local model first, which
        measured about six seconds of silence between pressing dial and the
        phone ringing. A host that survives its calls pays that once. Commands
        arrive as one JSON object per line; anything unparseable is reported and
        skipped rather than taking the host down mid-call.
        """

        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )
        while not self._stopping.is_set():
            line = await reader.readline()
            if not line:
                # Studio closed the pipe. Nothing can reach this host again.
                self._stopping.set()
                return
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            try:
                command = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("ignoring malformed host command")
                continue
            await self._handle_command(command)

    async def _handle_command(self, command: dict[str, Any]) -> None:
        name = str(command.get("command", "")).strip()
        if name == "dial":
            await self._command_dial(
                str(command.get("number", "")).strip(),
                recording_consent=bool(command.get("recording_consent", False)),
            )
        elif name == "hangup":
            runtime = self._runtime
            if runtime is not None:
                try:
                    await asyncio.to_thread(runtime.client.hangup)
                except Exception:
                    logger.warning("commanded hangup failed", exc_info=True)
        elif name == "shutdown":
            self._stopping.set()
        else:
            logger.warning("ignoring unknown host command %r", name)

    async def _command_dial(
        self, number: str, *, recording_consent: bool = False
    ) -> None:
        if not number:
            self._emit_event({"type": "call_error", "message": "dial command had no number"})
            return
        runtime = self._runtime
        if runtime is None:
            self._emit_event(
                {"type": "call_error", "message": "phone link is not ready for a dial"}
            )
            return
        os.environ["PHONE_AGENT_RECORDING_CONSENT"] = "true" if recording_consent else "false"
        os.environ["PHONE_AGENT_RECORDING_ENABLED"] = "true" if recording_consent else "false"
        # A warm host keeps per-call state between calls, so the identity of the
        # previous conversation has to be cleared or the next caller inherits
        # its greeting suppression and direction.
        self._outbound_number = number
        self._dial_number = None
        self.call_direction = "outbound"
        self._call_context_emitted = False
        self._outbound_seen_live_state = False
        self._greeting_attempted = False
        self._auto_answer_attempted = False
        self._active_caller_id = ""
        logger.info("placing outbound call to %s (recording_consent=%s)", number, recording_consent)
        try:
            response = await self._place_outbound_call(runtime, number)
        except Exception as exc:
            logger.exception("commanded dial failed")
            self._emit_event({"type": "call_error", "message": str(exc)})
            return
        if response.get("status") != "ok":
            logger.error("dial failed: %s", response)
            self._emit_event(
                {"type": "call_error", "message": str(response.get("message", "dial failed"))}
            )

    def _emit_event(self, event: dict[str, object]) -> None:
        if not getattr(self.config, "event_stream_enabled", False):
            return
        payload = dict(event)
        runtime = self._runtime
        if runtime is not None:
            payload.setdefault("call_id", str(runtime.session.call_id))
        if self._active_caller_id:
            payload.setdefault("caller_id", self._active_caller_id)
        payload.setdefault("direction", self.call_direction)
        payload.setdefault("channel", self.config.call_channel)
        print(
            "PHONE_AGENT_EVENT "
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            flush=True,
        )


async def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="PhoneAgent Voice Host")
    parser.add_argument(
        "--dial",
        help="Dial an outbound cellular number on startup and start AI conversation",
        default=None,
    )
    args = parser.parse_args()

    config = RuntimeConfig.from_env(require_provider_credentials=True)
    agent = PhoneVoiceAgent(config, dial_number=args.dial)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, agent.request_stop)
    try:
        await agent.run()
    except VoiceHostBusyError as exc:
        if config.event_stream_enabled:
            print(
                "PHONE_AGENT_EVENT " + json.dumps({"type": "call_error", "message": str(exc)}),
                flush=True,
            )
        raise
    except Exception as exc:
        # The direct WhatsApp sidecar has channel-specific setup failures (for
        # example PN -> LID resolution). Surface the real reason to Studio.
        # Other channels retain their existing exception path unchanged.
        if config.call_channel == "whatsapp" and config.event_stream_enabled:
            print(
                "PHONE_AGENT_EVENT "
                + json.dumps({"type": "call_error", "message": str(exc)}),
                flush=True,
            )
        raise


def main() -> None:
    # Pipecat uses Loguru and includes transcript text in DEBUG diagnostics.
    # Production defaults to INFO so content is not silently written to logs.
    pipecat_logger.remove()
    pipecat_logger.add(
        sys.stderr,
        level=os.getenv("PHONE_AGENT_PIPECAT_LOG_LEVEL", "INFO").strip().upper(),
    )
    try:
        asyncio.run(_main())
    except VoiceHostBusyError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
