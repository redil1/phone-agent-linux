"""A WhatsApp-Rust call presented as the same link the cellular path uses.

The AI transport binds four callbacks: caller audio in, agent audio out, an
end-of-utterance marker, and an urgent flush. This module supplies those over a
local Rust sidecar while the GSM path remains on its independent Android/PHAG
link.

Audio is PCM s16le 16 kHz mono in both directions. The AI side works in 20 ms
frames and WhatsApp-Rust in 60 ms frames, so this boundary re-chunks without
resampling. Python-to-Rust media is generation-framed so barge-in can clear both
queues and reject stale audio. Peer audio is raw PCM on stdout; status and
transport acknowledgements stay on stderr so logging can never corrupt audio.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("WhatsAppLink")

SAMPLE_RATE = 16_000
PHONE_FRAME_MS = 20
PHONE_FRAME_BYTES = SAMPLE_RATE * PHONE_FRAME_MS // 1000 * 2
WHATSAPP_FRAME_BYTES = 960 * 2
WHATSAPP_FRAME_SECONDS = 0.060

DEFAULT_BINARY = (
    Path(__file__).resolve().parents[1]
    / "whatsapp_channel"
    / "whatsapp-rust-caller"
)
ANSWER_TIMEOUT_SECS = 60.0
OUTPUT_QUEUE_FRAMES = 12

BRIDGE_MAGIC = b"WAR1"
BRIDGE_KIND_AUDIO = 1
BRIDGE_KIND_CONTROL = 2
BRIDGE_HEADER = struct.Struct("!4sBQQI")
BRIDGE_MAX_PAYLOAD = 64 * 1024
PLAYOUT_ACK_RE = re.compile(r"PLAYOUT_ACK generation=(\d+) sequence=(\d+)")
ANSWERED_MARKER = "[+] Peer ACCEPTED the call!"
SIDECAR_ERROR_MARKER = "Error:"

AudioListener = Callable[[bytes], None]
RenderAckHandler = Callable[[int, int], object]


@dataclass(frozen=True, slots=True)
class _OutboundFrame:
    pcm: bytes
    generation_id: int
    sequence: int


@dataclass(frozen=True, slots=True)
class _OutboundControl:
    payload: dict[str, object]
    generation_id: int
    sequence: int


class WhatsAppLinkError(RuntimeError):
    """The WhatsApp channel could not place or hold a call."""


def resolve_binary(configured: str | None = None) -> Path:
    configured = configured or os.getenv("PHONE_AGENT_WHATSAPP_BINARY", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path
        raise WhatsAppLinkError(f"WhatsApp caller not found at {path}")
    if DEFAULT_BINARY.is_file():
        return DEFAULT_BINARY
    found = shutil.which("whatsapp-rust-caller")
    if found:
        return Path(found)
    raise WhatsAppLinkError(
        f"WhatsApp-Rust caller not found at {DEFAULT_BINARY}. Build it with: "
        "cd whatsapp_channel/rust_caller && ./build.sh"
    )


async def is_paired(binary: Path | None = None) -> bool:
    """Whether the Rust sidecar has a persistent linked WhatsApp account."""

    try:
        path = binary or resolve_binary()
    except WhatsAppLinkError:
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            str(path),
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=20)
    except (TimeoutError, OSError):
        return False
    return b"status: logged_in" in stdout


class WhatsAppLink:
    """One outbound WhatsApp-Rust voice call shaped like the cellular link."""

    def __init__(
        self,
        *,
        binary: str | None = None,
        country_code: str = "212",
        max_duration_secs: int = 900,
        render_ack_handler: RenderAckHandler | None = None,
    ) -> None:
        self.binary = resolve_binary(binary)
        self.country_code = country_code
        self.max_duration_secs = max_duration_secs
        self._render_ack_handler = render_ack_handler
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._loop_thread_id = threading.get_ident()
        self._process: asyncio.subprocess.Process | None = None
        self._listeners: list[AudioListener] = []
        self._outbound: asyncio.Queue[_OutboundFrame | _OutboundControl | None] = asyncio.Queue(
            maxsize=OUTPUT_QUEUE_FRAMES
        )
        self._tasks: list[asyncio.Task] = []
        self._answered = asyncio.Event()
        self._ended = asyncio.Event()
        self._failure_message = ""
        self._inbound_remainder = bytearray()
        self._outbound_remainder = bytearray()
        self._outbound_remainder_generation = 0
        self._outbound_remainder_sequence = 0
        self._active_generation = 1
        self._stdin_lock = asyncio.Lock()
        self._running = False

    def on_audio_received(self, listener: AudioListener) -> None:
        self._listeners.append(listener)

    def send_audio_chunk(
        self, pcm_bytes: bytes, generation_id: int = 0, sequence: int = 0
    ) -> dict[str, object]:
        """Queue one 20 ms frame with its cancellation identity."""

        if not self._running or self._ended.is_set():
            return {"status": "closed", "accepted": 0}
        item = _OutboundFrame(bytes(pcm_bytes), generation_id, sequence)
        if not self._queue_ordered_item(item):
            return {"status": "dropped", "accepted": 0}
        return {"status": "ok", "accepted": len(pcm_bytes)}

    def _queue_ordered_item(
        self, item: _OutboundFrame | _OutboundControl
    ) -> bool:
        loop = self._loop
        if (
            loop is not None
            and loop.is_running()
            and not loop.is_closed()
            and threading.get_ident() != self._loop_thread_id
        ):
            try:
                asyncio.run_coroutine_threadsafe(self._outbound.put(item), loop).result(
                    timeout=2.0
                )
            except Exception:
                return False
        else:
            try:
                self._outbound.put_nowait(item)
            except asyncio.QueueFull:
                return False
        return True

    def send_audio_end_marker(
        self, generation_id: int = 0, sequence: int = 0
    ) -> dict[str, object]:
        if not self._running:
            return {"status": "closed"}
        queued = self._queue_ordered_item(
            _OutboundControl(
                {
                    "type": "audio_end",
                    "generation": generation_id,
                    "sequence": sequence,
                },
                generation_id,
                sequence,
            )
        )
        return {"status": "ok"} if queued else {"status": "dropped"}

    def flush_audio(self, advance_generation: object = True) -> dict[str, object]:
        """Drop Python and Rust source queues and advance their generation."""

        requested = getattr(advance_generation, "next_generation", None)
        if requested is None:
            requested = (
                self._active_generation + 1
                if advance_generation
                else self._active_generation
            )
        next_generation = max(int(requested), self._active_generation)
        loop = self._loop
        if (
            loop is not None
            and loop.is_running()
            and not loop.is_closed()
            and threading.get_ident() != self._loop_thread_id
        ):
            try:
                return asyncio.run_coroutine_threadsafe(
                    self._flush_async(next_generation), loop
                ).result(timeout=1.0)
            except Exception as exc:
                return {"status": "error", "message": str(exc), "dropped": 0}

        dropped = self._drain_outbound_now()
        self._active_generation = next_generation
        self._clear_outbound_remainder()
        return {
            "status": "ok",
            "dropped": dropped,
            "generation": next_generation,
        }

    async def _flush_async(self, next_generation: int) -> dict[str, object]:
        dropped = self._drain_outbound_now()
        self._active_generation = next_generation
        self._clear_outbound_remainder()
        if self._running:
            await self._write_control(
                {"type": "flush", "next_generation": next_generation},
                next_generation,
                0,
            )
        logger.info(
            "WhatsApp flush generation=%d dropped=%d queued frame(s)",
            next_generation,
            dropped,
        )
        return {
            "status": "ok",
            "dropped": dropped,
            "generation": next_generation,
        }

    def _drain_outbound_now(self) -> int:
        dropped = 0
        while not self._outbound.empty():
            try:
                self._outbound.get_nowait()
                self._outbound.task_done()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        return dropped

    def _clear_outbound_remainder(self) -> None:
        self._outbound_remainder.clear()
        self._outbound_remainder_generation = 0
        self._outbound_remainder_sequence = 0

    async def dial(self, phone_number: str) -> None:
        if self._running:
            raise WhatsAppLinkError("this link already has a call in progress")
        command = [
            str(self.binary),
            "call",
            "--country-code",
            self.country_code,
            "--duration",
            str(self.max_duration_secs),
            "--framed-stdio",
            phone_number,
        ]
        logger.info("dialling %s over WhatsApp-Rust", phone_number)
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._loop = asyncio.get_running_loop()
        self._loop_thread_id = threading.get_ident()
        self._answered.clear()
        self._ended.clear()
        self._failure_message = ""
        self._active_generation = 1
        self._inbound_remainder.clear()
        self._clear_outbound_remainder()
        self._running = True
        self._tasks = [
            asyncio.create_task(self._read_peer_audio(), name="whatsapp-rust-downlink"),
            asyncio.create_task(self._write_agent_audio(), name="whatsapp-rust-uplink"),
            asyncio.create_task(self._watch_status(), name="whatsapp-rust-status"),
        ]

        answered = asyncio.create_task(self._answered.wait())
        ended = asyncio.create_task(self._ended.wait())
        try:
            done, _ = await asyncio.wait(
                {answered, ended},
                timeout=ANSWER_TIMEOUT_SECS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if answered not in done or not self._answered.is_set():
                if self._failure_message:
                    raise WhatsAppLinkError(self._failure_message)
                raise TimeoutError
        except WhatsAppLinkError:
            await self.hangup()
            raise
        except TimeoutError as exc:
            await self.hangup()
            raise WhatsAppLinkError("the WhatsApp call was not answered") from exc
        finally:
            for waiter in (answered, ended):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(answered, ended, return_exceptions=True)

    async def hangup(self) -> None:
        process = self._process
        if process is not None and process.returncode is None and self._running:
            try:
                await self._write_control(
                    {"type": "hangup", "reason": "phoneagent"},
                    self._active_generation,
                    0,
                )
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except Exception:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()

        self._running = False
        self._ended.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._process = None
        self._drain_outbound_now()
        self._clear_outbound_remainder()

    @property
    def answered(self) -> bool:
        return self._answered.is_set()

    @property
    def ended(self) -> bool:
        return self._ended.is_set()

    async def _read_peer_audio(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        stream = self._process.stdout
        while self._running:
            chunk = await stream.read(WHATSAPP_FRAME_BYTES)
            if not chunk:
                break
            self._inbound_remainder.extend(chunk)
            while len(self._inbound_remainder) >= PHONE_FRAME_BYTES:
                frame = bytes(self._inbound_remainder[:PHONE_FRAME_BYTES])
                del self._inbound_remainder[:PHONE_FRAME_BYTES]
                for listener in self._listeners:
                    try:
                        listener(frame)
                    except Exception:
                        logger.warning("WhatsApp audio listener failed", exc_info=True)
        self._ended.set()

    async def _write_agent_audio(self) -> None:
        assert self._process is not None and self._process.stdin is not None
        next_write_at = asyncio.get_running_loop().time()
        while self._running:
            item = await self._outbound.get()
            try:
                if item is None:
                    return
                if isinstance(item, _OutboundControl):
                    if item.generation_id != self._active_generation:
                        continue
                    if self._outbound_remainder:
                        if self._outbound_remainder_generation != item.generation_id:
                            self._clear_outbound_remainder()
                        else:
                            final_audio_sequence = self._outbound_remainder_sequence
                            padded = bytes(self._outbound_remainder).ljust(
                                WHATSAPP_FRAME_BYTES, b"\x00"
                            )
                            self._clear_outbound_remainder()
                            next_write_at = await self._write_paced_audio(
                                padded,
                                item.generation_id,
                                final_audio_sequence,
                                next_write_at,
                            )
                    await self._write_control(
                        item.payload,
                        item.generation_id,
                        item.sequence,
                    )
                    continue
                if item.generation_id != self._active_generation:
                    continue
                if self._outbound_remainder_generation not in (
                    0,
                    item.generation_id,
                ):
                    self._clear_outbound_remainder()
                self._outbound_remainder_generation = item.generation_id
                self._outbound_remainder_sequence = item.sequence
                self._outbound_remainder.extend(item.pcm)
                while len(self._outbound_remainder) >= WHATSAPP_FRAME_BYTES:
                    frame = bytes(self._outbound_remainder[:WHATSAPP_FRAME_BYTES])
                    del self._outbound_remainder[:WHATSAPP_FRAME_BYTES]
                    next_write_at = await self._write_paced_audio(
                        frame,
                        self._outbound_remainder_generation,
                        self._outbound_remainder_sequence,
                        next_write_at,
                    )
            except (BrokenPipeError, ConnectionResetError):
                self._ended.set()
                return
            finally:
                self._outbound.task_done()

    async def _write_paced_audio(
        self,
        frame: bytes,
        generation_id: int,
        sequence: int,
        next_write_at: float,
    ) -> float:
        now = asyncio.get_running_loop().time()
        if now < next_write_at:
            await asyncio.sleep(next_write_at - now)
        await self._write_bridge_frame(
            BRIDGE_KIND_AUDIO,
            generation_id,
            sequence,
            frame,
        )
        return max(next_write_at + WHATSAPP_FRAME_SECONDS, now)

    async def _write_control(
        self, payload: dict[str, object], generation_id: int, sequence: int
    ) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        await self._write_bridge_frame(
            BRIDGE_KIND_CONTROL,
            generation_id,
            sequence,
            encoded,
        )

    async def _write_bridge_frame(
        self,
        kind: int,
        generation_id: int,
        sequence: int,
        payload: bytes,
    ) -> None:
        if len(payload) > BRIDGE_MAX_PAYLOAD:
            raise WhatsAppLinkError("WhatsApp bridge payload is too large")
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise BrokenPipeError("WhatsApp-Rust sidecar is not accepting media")
        header = BRIDGE_HEADER.pack(
            BRIDGE_MAGIC,
            kind,
            max(0, int(generation_id)),
            max(0, int(sequence)),
            len(payload),
        )
        async with self._stdin_lock:
            process.stdin.write(header)
            process.stdin.write(payload)
            await process.stdin.drain()

    async def _watch_status(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        stream = self._process.stderr
        while self._running:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            logger.info("whatsapp-rust: %s", text)
            rendered = PLAYOUT_ACK_RE.search(text)
            if rendered is not None and self._render_ack_handler is not None:
                try:
                    self._render_ack_handler(
                        int(rendered.group(1)),
                        int(rendered.group(2)),
                    )
                except Exception:
                    logger.warning("WhatsApp transport ACK handler failed", exc_info=True)
            if text.startswith(SIDECAR_ERROR_MARKER):
                self._failure_message = text.split(SIDECAR_ERROR_MARKER, 1)[1].strip()
            if ANSWERED_MARKER in text:
                self._answered.set()
            if "Call ended" in text or "TERMINATED" in text:
                self._ended.set()
                break
        self._ended.set()


PAIRING_CODE_MARKER = "YOUR PAIRING CODE:"
PAIRING_TIMEOUT_SECS = 180.0


async def pair_phone(
    phone_number: str,
    *,
    country_code: str = "212",
    binary: str | None = None,
    on_code: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Link the Rust sidecar to a WhatsApp account with a phone code.

    Pairing cannot complete from the Studio alone: the code is useless without
    the phone it must be typed into under WhatsApp's Linked Devices screen.
    """

    path = resolve_binary(binary)
    process = await asyncio.create_subprocess_exec(
        str(path),
        "pair-phone",
        phone_number,
        "--country-code",
        country_code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    code = ""
    paired = False
    assert process.stdout is not None
    try:
        async with asyncio.timeout(PAIRING_TIMEOUT_SECS):
            while line := await process.stdout.readline():
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                logger.info("pairing: %s", text)
                if PAIRING_CODE_MARKER in text:
                    code = text.split(PAIRING_CODE_MARKER, 1)[1].strip()
                    if on_code is not None:
                        result = on_code(code)
                        if asyncio.iscoroutine(result):
                            await result
                if "Pairing complete" in text:
                    paired = True
                    break
            await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()
        return {
            "paired": False,
            "code": code,
            "error": "the code was not entered on the phone in time",
        }
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    return {
        "paired": paired,
        "code": code,
        "error": "" if paired else "pairing did not complete",
    }
