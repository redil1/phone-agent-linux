"""Antigravity Live STT Service for Pipecat.

Streams 16kHz PCM audio from the cellular downlink into Google's speech
recognition engine via the local Antigravity Language Server bridge.
Includes adaptive silence-watchdog endpointing for sub-second turn latency.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import math
import re
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import STTService

from .turn_continuity import looks_semantically_incomplete

logger = logging.getLogger("AntigravityLiveSTT")

SERVICE = "exa.language_server_pb.LanguageServerService"
CONNECT_JSON = "application/connect+json"
APP_JSON = "application/json"

SpeculationCandidateHandler = Callable[[str], Awaitable[None] | None]
SpeculationCancelHandler = Callable[[str], Awaitable[None] | None]

_ENGLISH_LANGUAGE_MARKERS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "can",
        "do",
        "for",
        "hello",
        "how",
        "i",
        "is",
        "like",
        "matches",
        "no",
        "please",
        "thanks",
        "the",
        "this",
        "watch",
        "we",
        "what",
        "why",
        "with",
        "would",
        "yes",
        "you",
        "your",
    }
)
_FRENCH_LANGUAGE_MARKERS = frozenset(
    {
        "avec",
        "bonjour",
        "comment",
        "dans",
        "de",
        "des",
        "du",
        "est",
        "et",
        "je",
        "la",
        "le",
        "les",
        "matchs",
        "merci",
        "non",
        "nous",
        "oui",
        "pour",
        "pourquoi",
        "que",
        "qui",
        "sur",
        "une",
        "vous",
        "votre",
    }
)


def _calc_dbfs(audio: bytes) -> float:
    """Calculate RMS dBFS of 16-bit mono PCM."""
    if len(audio) < 2:
        return -120.0
    samples = memoryview(audio).cast("h")
    if not samples:
        return -120.0
    sum_sq = sum(s * s for s in samples)
    mean_sq = sum_sq / len(samples)
    if mean_sq <= 0:
        return -120.0
    return 20.0 * math.log10(math.sqrt(mean_sq) / 32768.0)


class _StreamConn:
    """De-chunks Connect-gRPC server streams over raw TLS."""

    def __init__(self, sock: ssl.SSLSocket):
        self.sock = sock
        self.status = 0
        self.headers: dict[str, str] = {}
        self._rbuf = bytearray()
        self._body = bytearray()
        self._chunk_left = 0
        self._eof = False
        self._read_head()

    def _read_head(self) -> None:
        while b"\r\n\r\n" not in self._rbuf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("Stream closed before HTTP head")
            self._rbuf.extend(chunk)
        head, rest = self._rbuf.split(b"\r\n\r\n", 1)
        self._rbuf = bytearray(rest)
        lines = head.split(b"\r\n")
        parts = lines[0].split(b" ", 2)
        self.status = int(parts[1]) if len(parts) > 1 else 0
        for ln in lines[1:]:
            if b":" in ln:
                k, v = ln.split(b":", 1)
                self.headers[k.decode("latin1").strip().lower()] = v.decode("latin1").strip()

    def _fill(self, want: int) -> bool:
        while len(self._body) < want and not self._eof:
            if self._chunk_left <= 0:
                while b"\r\n" not in self._rbuf:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        self._eof = True
                        return len(self._body) >= want
                    self._rbuf.extend(chunk)
                line, rest = self._rbuf.split(b"\r\n", 1)
                self._rbuf = bytearray(rest)
                if not line.strip():
                    continue
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    continue
                if size == 0:
                    self._eof = True
                    break
                self._chunk_left = size
            take = min(want - len(self._body), self._chunk_left)
            while len(self._rbuf) < take and not self._eof:
                chunk = self.sock.recv(4096)
                if not chunk:
                    self._eof = True
                    break
                self._rbuf.extend(chunk)
            actual = min(len(self._rbuf), take)
            self._body.extend(self._rbuf[:actual])
            del self._rbuf[:actual]
            self._chunk_left -= actual
        return len(self._body) >= want

    def read_envelope(self, timeout: float = 1.0) -> tuple[int | None, bytes | None]:
        self.sock.settimeout(timeout)
        try:
            if not self._fill(5):
                return None, None
            head = bytes(self._body[:5])
            del self._body[:5]
            flag = head[0]
            length = struct.unpack(">I", head[1:5])[0]
            if not self._fill(length):
                return None, None
            payload = bytes(self._body[:length])
            del self._body[:length]
            return flag, payload
        except TimeoutError:
            return None, None

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class AntigravityLiveSTTService(STTService):
    """Production Pipecat STT Adapter for Antigravity's Live Speech Stream."""

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        language: str = "en-US",
        chunk_duration_ms: int = 150,
        silence_endpoint_ms: int = 900,
        incomplete_endpoint_ms: int = 1500,
        transcript_stability_ms: int = 280,
        fallback_endpoint_ms: int = 1800,
        barge_in_min_ms: int = 220,
        energy_threshold_dbfs: float = -42.0,
        context_bias: str = "",
        speculative_pipeline_enabled: bool = False,
        speculative_prefetch_silence_ms: int = 180,
        speculative_prefetch_stability_ms: int = 140,
        speculative_fast_endpoint_ms: int = 450,
        speculative_ambiguous_endpoint_ms: int = 700,
        speculative_incomplete_endpoint_ms: int = 1100,
        base_url: str = "",
        csrf_token: str = "",
        **kwargs: Any,
    ) -> None:
        # Crucial: audio_passthrough=False ensures caller voice NEVER echoes into output transport
        super().__init__(
            audio_passthrough=False,
            sample_rate=sample_rate,
            settings=STTSettings(model=None, language=language),
            **kwargs,
        )
        self._target_sample_rate = sample_rate
        self._language = language
        self._chunk_bytes = max(640, int(sample_rate * 2 * (chunk_duration_ms / 1000.0)))
        self._silence_endpoint_sec = silence_endpoint_ms / 1000.0
        self._incomplete_endpoint_sec = incomplete_endpoint_ms / 1000.0
        self._transcript_stability_sec = transcript_stability_ms / 1000.0
        self._fallback_endpoint_sec = fallback_endpoint_ms / 1000.0
        self._barge_in_min_sec = barge_in_min_ms / 1000.0
        self._energy_threshold_dbfs = energy_threshold_dbfs
        self._context_bias = context_bias
        self._speculative_pipeline_enabled = speculative_pipeline_enabled
        self._speculative_prefetch_silence_sec = speculative_prefetch_silence_ms / 1000.0
        self._speculative_prefetch_stability_sec = speculative_prefetch_stability_ms / 1000.0
        self._speculative_fast_endpoint_sec = speculative_fast_endpoint_ms / 1000.0
        self._speculative_ambiguous_endpoint_sec = speculative_ambiguous_endpoint_ms / 1000.0
        self._speculative_incomplete_endpoint_sec = speculative_incomplete_endpoint_ms / 1000.0
        self._speculation_candidate_handler: SpeculationCandidateHandler | None = None
        self._speculation_cancel_handler: SpeculationCancelHandler | None = None
        self._last_speculation_text = ""
        self._audio_buffer = bytearray()
        self._seq = 0
        self._session_id: str | None = None
        self._stream: _StreamConn | None = None
        self._reader_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._sender_task: asyncio.Task | None = None
        self._send_queue: asyncio.Queue[tuple[bytes, int]] = asyncio.Queue(maxsize=64)
        self._transcript_lock = asyncio.Lock()
        self._base_url = base_url
        self._csrf_token = csrf_token
        self._ssl_ctx = self._create_ssl_context()
        self._speaking = False
        self._last_speech_at = 0.0
        self._last_transcript = ""
        self._final_transcript = ""
        self._provider_final_seen = False
        self._last_transcript_update_at = 0.0
        self._last_committed_text = ""
        self._last_committed_at = 0.0
        self._last_committed_speech_epoch = -1
        self._speech_epoch = 0
        self._candidate_speech_epoch = 0
        self._speech_burst_active = False
        self._late_revision_window_sec = 2.5
        self._language_switch_endpoint_sec = 1.6
        self._speech_candidate_at = 0.0
        self._is_closing = False

    def set_speculation_handlers(
        self,
        candidate_handler: SpeculationCandidateHandler | None,
        cancel_handler: SpeculationCancelHandler | None,
    ) -> None:
        """Attach optional non-blocking speculative turn orchestration hooks."""

        self._speculation_candidate_handler = candidate_handler
        self._speculation_cancel_handler = cancel_handler

    async def _invoke_speculation_handler(
        self,
        handler: SpeculationCandidateHandler | SpeculationCancelHandler | None,
        value: str,
    ) -> None:
        if handler is None:
            return
        result = handler(value)
        if inspect.isawaitable(result):
            await result

    def _create_ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _discover_bridge(self) -> None:
        if self._base_url and self._csrf_token:
            return
        for port in range(53850, 53872):
            try:
                url = f"https://127.0.0.1:{port}/"
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=1.5) as r:
                    html = r.read().decode(errors="replace")
                m = re.search(r'csrfToken":"([^"]+)"', html)
                if m and "antigravity" in html:
                    self._base_url = f"https://127.0.0.1:{port}"
                    self._csrf_token = m.group(1)
                    logger.info("Found Antigravity bridge on %s", self._base_url)
                    return
            except Exception:
                continue
        raise RuntimeError("Antigravity language_server bridge not found on 127.0.0.1:53850-53872")

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if not self._base_url or not self._csrf_token:
            await asyncio.to_thread(self._discover_bridge)
        await self._start_session()

    async def _start_session(self) -> None:
        self._seq = 0
        self._audio_buffer.clear()
        self._speaking = False
        self._last_speech_at = time.monotonic()
        self._last_transcript = ""
        self._final_transcript = ""
        self._provider_final_seen = False
        self._last_transcript_update_at = 0.0
        self._last_committed_text = ""
        self._last_committed_at = 0.0
        self._last_committed_speech_epoch = -1
        self._speech_epoch = 0
        self._candidate_speech_epoch = 0
        self._speech_burst_active = False
        self._speech_candidate_at = 0.0
        self._last_speculation_text = ""
        self._send_queue = asyncio.Queue(maxsize=64)
        self._is_closing = False

        body: dict[str, Any] = {
            "mimeType": "audio/l16;rate=16000;channels=1",
            "continuous": True,
            "language": self._language or "en-US",
        }
        if self._context_bias:
            body["preCursorText"] = self._context_bias

        payload = json.dumps(body).encode()
        envelope = b"\x00" + struct.pack(">I", len(payload)) + payload

        url = urllib.parse.urlsplit(self._base_url)
        host, port = url.hostname or "127.0.0.1", url.port or 53857

        def open_socket() -> _StreamConn:
            sock = socket.create_connection((host, port), timeout=5)
            tls = self._ssl_ctx.wrap_socket(sock, server_hostname=host)
            headers = {
                "Content-Type": CONNECT_JSON,
                "Accept": CONNECT_JSON,
                "x-codeium-csrf-token": self._csrf_token,
                "Origin": self._base_url,
                "Content-Length": str(len(envelope)),
            }
            head = (
                f"POST /{SERVICE}/StreamAudioTranscription HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                + "\r\n"
            ).encode()
            tls.sendall(head + envelope)
            return _StreamConn(tls)

        self._stream = await asyncio.to_thread(open_socket)
        if self._stream.status != 200:
            raise RuntimeError(f"StreamAudioTranscription failed HTTP {self._stream.status}")

        _, data = await asyncio.to_thread(self._stream.read_envelope, 10.0)
        if not data:
            raise RuntimeError("No ready envelope received from bridge")
        ready_obj = json.loads(data).get("ready", {})
        self._session_id = ready_obj.get("sessionId")
        if not self._session_id:
            raise RuntimeError("Missing sessionId in ready handshake")

        logger.info("Antigravity Live STT Session started: %s", self._session_id)
        self._reader_task = asyncio.create_task(self._stream_reader_loop(), name="ag_stt_reader")
        self._watchdog_task = asyncio.create_task(
            self._silence_watchdog_loop(), name="ag_stt_watchdog"
        )
        self._sender_task = asyncio.create_task(self._audio_sender_loop(), name="ag_stt_sender")

    async def _stream_reader_loop(self) -> None:
        """Continuously reads incoming transcription envelopes and pushes Pipecat frames."""
        try:
            while self._stream is not None and not self._is_closing:
                _, payload = await asyncio.to_thread(self._stream.read_envelope, 0.1)
                if not payload:
                    continue
                obj = json.loads(payload)
                if "transcription" in obj:
                    t = obj["transcription"]
                    text = t.get("text", "").strip()
                    is_final = bool(t.get("isFinal"))
                    if text:
                        async with self._transcript_lock:
                            candidate = self._stage_transcription(text, is_final=is_final)
                        if candidate:
                            await self._ensure_user_started(force=False)
                            await self.push_frame(
                                InterimTranscriptionFrame(
                                    text=candidate,
                                    user_id="caller",
                                    timestamp=None,
                                )
                            )
                elif "complete" in obj:
                    await self._commit_pending_transcript(
                        source="stream_complete",
                        require_provider_final=True,
                    )
                    break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self._is_closing:
                logger.exception("Error in Antigravity STT reader loop: %s", exc)
                await self.push_frame(ErrorFrame(error=f"Antigravity STT reader error: {exc}"))

    async def _ensure_user_started(self, *, force: bool) -> None:
        if self._speaking or self._speech_candidate_at == 0.0:
            return
        candidate_age = time.monotonic() - self._speech_candidate_at
        if force or candidate_age >= self._barge_in_min_sec:
            self._speaking = True
            await self.push_frame(UserStartedSpeakingFrame())

    @staticmethod
    def _merge_transcript(left: str, right: str) -> str:
        """Merge cumulative and segmented provider revisions without duplicating words."""

        left = " ".join(left.split())
        right = " ".join(right.split())
        if not left:
            return right
        if not right or right == left:
            return left
        if right.startswith(left):
            return right
        if left.startswith(right):
            return left
        left_words = left.split()
        right_words = right.split()
        left_keys = [re.sub(r"^\W+|\W+$", "", word).casefold() for word in left_words]
        right_keys = [re.sub(r"^\W+|\W+$", "", word).casefold() for word in right_words]
        for overlap in range(min(len(left_words), len(right_words)), 0, -1):
            if left_keys[-overlap:] == right_keys[:overlap]:
                return " ".join([*left_words, *right_words[overlap:]])
        return f"{left} {right}"

    @staticmethod
    def _language_signal(text: str) -> str:
        tokens = re.findall(r"[a-zà-ÿ']+", text.casefold())
        english = sum(token in _ENGLISH_LANGUAGE_MARKERS for token in tokens)
        french = sum(token in _FRENCH_LANGUAGE_MARKERS for token in tokens)
        if re.search(r"[àâçéèêëîïôùûüÿœ]", text.casefold()):
            french += 2
        if english >= 2 and french >= 2:
            return "mixed"
        if english >= 2 and english > french:
            return "en"
        if french >= 2 and french > english:
            return "fr"
        return "unknown"

    @staticmethod
    def _content_overlap(left: str, right: str) -> int:
        def content_words(value: str) -> set[str]:
            words = set(re.findall(r"[a-zà-ÿ']+", value.casefold()))
            return {
                word
                for word in words
                if len(word) >= 4
                and word not in _ENGLISH_LANGUAGE_MARKERS
                and word not in _FRENCH_LANGUAGE_MARKERS
            }

        left_words = content_words(left)
        right_words = content_words(right)
        return len(left_words & right_words)

    def _has_language_conflict(self, text: str) -> bool:
        signal = self._language_signal(text)
        expected = "fr" if self._language.lower().startswith("fr") else "en"
        return signal in {"en", "fr", "mixed"} and signal != expected

    def _track_speech_energy(self, dbfs: float) -> None:
        now = time.monotonic()
        if dbfs >= self._energy_threshold_dbfs:
            if not self._speech_burst_active:
                self._speech_epoch += 1
                self._speech_burst_active = True
            self._last_speech_at = now
        elif self._speech_burst_active and now - self._last_speech_at >= 0.30:
            self._speech_burst_active = False

    def _stage_transcription(self, text: str, *, is_final: bool) -> str:
        now = time.monotonic()
        text = " ".join(text.split())
        if not text:
            return ""

        # A continuous provider can revise one acoustic utterance after our
        # endpoint timer committed. Without new caller speech, every such value
        # is a correction of the previous turn—not a second caller turn.
        if (
            now - self._last_committed_at < self._late_revision_window_sec
            and self._last_committed_text
        ):
            same_acoustic_turn = self._speech_epoch <= self._last_committed_speech_epoch
            if same_acoustic_turn:
                if text != self._last_committed_text:
                    logger.warning(
                        "Suppressed late STT revision without new speech epoch=%d chars=%d",
                        self._speech_epoch,
                        len(text),
                    )
                return ""
            if text == self._last_committed_text:
                return ""
            if text.startswith(self._last_committed_text):
                text = text[len(self._last_committed_text) :].strip(" ,.;:?!")
                if not text:
                    return ""

        if self._speech_candidate_at == 0.0:
            self._speech_candidate_at = now
            self._candidate_speech_epoch = self._speech_epoch

        previous = self._last_transcript
        if is_final:
            previous_signal = self._language_signal(self._final_transcript)
            new_signal = self._language_signal(text)
            is_cross_language_revision = (
                bool(self._final_transcript)
                and previous_signal in {"en", "fr"}
                and new_signal in {"en", "fr"}
                and previous_signal != new_signal
                and self._content_overlap(self._final_transcript, text) >= 2
            )
            if is_cross_language_revision:
                logger.info(
                    "Replacing cross-language STT hypothesis %s->%s in speech epoch %d",
                    previous_signal,
                    new_signal,
                    self._candidate_speech_epoch,
                )
                self._final_transcript = text
            else:
                self._final_transcript = self._merge_transcript(self._final_transcript, text)
            candidate = self._final_transcript
            self._provider_final_seen = True
        else:
            candidate = self._merge_transcript(self._final_transcript, text)

        self._last_transcript = candidate
        if candidate != previous or is_final:
            self._last_transcript_update_at = now
        return candidate

    @staticmethod
    def _looks_incomplete(text: str) -> bool:
        return looks_semantically_incomplete(text)

    def _required_silence(self) -> float:
        text = self._last_transcript.strip()
        language_conflict = self._has_language_conflict(text)
        if self._speculative_pipeline_enabled:
            if self._looks_incomplete(text):
                required = self._speculative_incomplete_endpoint_sec
            elif text.endswith((".", "!", "?")) and self._provider_final_seen:
                required = self._speculative_fast_endpoint_sec
            elif self._provider_final_seen:
                required = self._speculative_ambiguous_endpoint_sec
            else:
                required = self._fallback_endpoint_sec
            if not self._provider_final_seen:
                required = max(required, self._fallback_endpoint_sec)
            if language_conflict:
                required = max(required, self._language_switch_endpoint_sec)
            return required
        if self._looks_incomplete(text):
            required = max(self._silence_endpoint_sec, self._incomplete_endpoint_sec)
        elif text.endswith((".", "!", "?")):
            required = max(0.55, self._silence_endpoint_sec * 0.75)
        else:
            required = self._silence_endpoint_sec
        if not self._provider_final_seen:
            required = max(required, self._fallback_endpoint_sec)
        return required

    async def _commit_pending_transcript(
        self,
        *,
        source: str,
        require_provider_final: bool = False,
        expected_update_at: float | None = None,
    ) -> None:
        async with self._transcript_lock:
            if require_provider_final and not self._provider_final_seen:
                return
            if (
                expected_update_at is not None
                and expected_update_at != self._last_transcript_update_at
            ):
                return
            text = self._last_transcript.strip()
            if not text:
                return
            provider_final_seen = self._provider_final_seen
            emit_user_started = not self._speaking
            if emit_user_started:
                self._speaking = True
            self._last_committed_text = text
            self._last_committed_at = time.monotonic()
            self._last_committed_speech_epoch = self._candidate_speech_epoch
            self._last_transcript = ""
            self._final_transcript = ""
            self._provider_final_seen = False
            self._last_transcript_update_at = 0.0
            self._speech_candidate_at = 0.0
            self._candidate_speech_epoch = self._speech_epoch
            self._last_speculation_text = ""

        if emit_user_started:
            await self.push_frame(UserStartedSpeakingFrame())
        await self.push_frame(
            TranscriptionFrame(
                text=text,
                user_id="caller",
                timestamp=None,
            )
        )
        if self._speaking:
            self._speaking = False
            await self.push_frame(UserStoppedSpeakingFrame())
        logger.info(
            "Committed stable caller turn source=%s chars=%d provider_final=%s",
            source,
            len(text),
            provider_final_seen,
        )

    async def _silence_watchdog_loop(self) -> None:
        """Commit one stable, complete turn after a natural silence pause."""
        try:
            while not self._is_closing:
                await asyncio.sleep(0.03)
                if not self._last_transcript.strip():
                    continue
                await self._ensure_user_started(force=False)
                now = time.monotonic()
                silence_elapsed = now - self._last_speech_at
                stable_elapsed = now - self._last_transcript_update_at
                if (
                    self._speculative_pipeline_enabled
                    and self._last_transcript != self._last_speculation_text
                    and not self._looks_incomplete(self._last_transcript)
                    and silence_elapsed >= self._speculative_prefetch_silence_sec
                    and stable_elapsed >= self._speculative_prefetch_stability_sec
                ):
                    self._last_speculation_text = self._last_transcript
                    await self._invoke_speculation_handler(
                        self._speculation_candidate_handler,
                        self._last_speculation_text,
                    )
                if (
                    silence_elapsed >= self._required_silence()
                    and stable_elapsed >= self._transcript_stability_sec
                ):
                    await self._commit_pending_transcript(
                        source="adaptive_silence",
                        expected_update_at=self._last_transcript_update_at,
                    )
        except asyncio.CancelledError:
            pass

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Buffer and send audio chunks while updating speech activity energy."""
        # Energy detector for adaptive turn endpointing and acoustic turn identity.
        db = _calc_dbfs(audio)
        if db >= self._energy_threshold_dbfs:
            if self._last_speculation_text:
                self._last_speculation_text = ""
                await self._invoke_speculation_handler(
                    self._speculation_cancel_handler,
                    "speech_resumed",
                )
        self._track_speech_energy(db)

        self._audio_buffer.extend(audio)
        while len(self._audio_buffer) >= self._chunk_bytes and self._session_id:
            chunk = bytes(self._audio_buffer[: self._chunk_bytes])
            del self._audio_buffer[: self._chunk_bytes]
            seq = self._seq
            self._seq += 1
            if self._sender_task is None:
                await self._send_chunk(chunk, seq)
            else:
                await self._send_queue.put((chunk, seq))

        if False:
            yield None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, CancelFrame | EndFrame):
            await self._close_session()

    async def _send_chunk(self, data: bytes, seq: int) -> None:
        if not self._session_id or self._is_closing:
            return
        b64_data = base64.b64encode(data).decode()
        body = {
            "sessionId": self._session_id,
            "data": b64_data,
            "sequenceNumber": seq,
        }

        def send_unary() -> None:
            req = urllib.request.Request(
                f"{self._base_url}/{SERVICE}/SendAudioChunk",
                data=json.dumps(body).encode(),
                method="POST",
                headers={
                    "Content-Type": APP_JSON,
                    "Accept": APP_JSON,
                    "x-codeium-csrf-token": self._csrf_token,
                    "Origin": self._base_url,
                },
            )
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=2) as r:
                r.read()

        try:
            await asyncio.to_thread(send_unary)
        except Exception as exc:
            if not self._is_closing:
                logger.warning("SendAudioChunk seq=%d failed: %s", seq, exc)

    async def _audio_sender_loop(self) -> None:
        """Keep bridge HTTP latency out of the real-time Pipecat audio callback."""
        try:
            while not self._is_closing:
                data, seq = await self._send_queue.get()
                try:
                    await self._send_chunk(data, seq)
                finally:
                    self._send_queue.task_done()
        except asyncio.CancelledError:
            pass

    async def stop(self, frame: EndFrame) -> None:
        await self._close_session()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        await self._close_session()
        await super().cancel(frame)

    async def cleanup(self) -> None:
        await self._close_session()
        await super().cleanup()

    async def _close_session(self) -> None:
        self._is_closing = True
        if self._last_speculation_text:
            self._last_speculation_text = ""
            await self._invoke_speculation_handler(
                self._speculation_cancel_handler,
                "stt_session_closed",
            )
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None

        if self._sender_task:
            self._sender_task.cancel()
            await asyncio.gather(self._sender_task, return_exceptions=True)
            self._sender_task = None

        if self._reader_task:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None

        if self._session_id:
            sid = self._session_id
            self._session_id = None

            def end_unary() -> None:
                try:
                    req = urllib.request.Request(
                        f"{self._base_url}/{SERVICE}/EndAudioSession",
                        data=json.dumps({"sessionId": sid}).encode(),
                        method="POST",
                        headers={
                            "Content-Type": APP_JSON,
                            "Accept": APP_JSON,
                            "x-codeium-csrf-token": self._csrf_token,
                            "Origin": self._base_url,
                        },
                    )
                    with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=2) as r:
                        r.read()
                except Exception:
                    pass

            await asyncio.to_thread(end_unary)

        if self._stream:
            self._stream.close()
            self._stream = None
