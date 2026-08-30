"""Consent-gated, nonblocking call recording at the generic media boundary."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import stat
import struct
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

DEFAULT_RECORDING_ROOT = Path.home() / ".local" / "share" / "phone-agent" / "recordings"
Direction = Literal["remote", "agent"]


class RecordingError(RuntimeError):
    """A recording could not be safely created or finalized."""


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RecordingError(f"{name} must be true or false")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RecordingError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RecordingError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class RecordingConfig:
    enabled: bool = False
    consent_granted: bool = False
    root: Path = DEFAULT_RECORDING_ROOT
    retention_days: int = 30
    queue_frames: int = 2_000

    @classmethod
    def from_env(cls) -> RecordingConfig:
        root = Path(
            os.getenv("PHONE_AGENT_RECORDING_ROOT", "").strip() or DEFAULT_RECORDING_ROOT
        ).expanduser()
        if not root.is_absolute():
            raise RecordingError("PHONE_AGENT_RECORDING_ROOT must be absolute")
        return cls(
            enabled=_env_bool("PHONE_AGENT_RECORDING_ENABLED", False),
            consent_granted=_env_bool("PHONE_AGENT_RECORDING_CONSENT", False),
            root=root,
            retention_days=_env_int("PHONE_AGENT_RECORDING_RETENTION_DAYS", 30, 1, 3650),
            queue_frames=_env_int("PHONE_AGENT_RECORDING_QUEUE_FRAMES", 2_000, 100, 20_000),
        )


@dataclass(frozen=True, slots=True)
class RecordingResult:
    recording_id: str
    directory: Path
    manifest: Path
    complete: bool
    dropped_frames: int


@dataclass(frozen=True, slots=True)
class _AudioItem:
    direction: Direction
    captured_ns: int
    pcm: bytes


class CallRecordingSession:
    """Write two aligned PCM tracks and a mixed conversation off the audio thread."""

    def __init__(
        self,
        *,
        call_id: str,
        caller_id: str,
        channel: str,
        sample_rate: int = 16_000,
        config: RecordingConfig | None = None,
    ) -> None:
        self.config = config or RecordingConfig.from_env()
        if not self.config.enabled or not self.config.consent_granted:
            raise RecordingError("recording requires both enablement and per-call consent")
        if sample_rate < 8_000 or sample_rate > 48_000:
            raise RecordingError("recording sample rate is unsupported")
        self.sample_rate = sample_rate
        self.channel = str(channel)[:32]
        self._started_ns = time.monotonic_ns()
        identity = f"{call_id}:{caller_id}:{self._started_ns}"
        self.recording_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        self._subject = "sha256:" + hashlib.sha256(str(caller_id).encode()).hexdigest()[:16]
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self.directory = self.config.root / f"{stamp}-{self.recording_id}"
        self._validate_root()
        self.directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        os.chmod(self.directory, 0o700)
        self.remote_path = self.directory / "remote.wav"
        self.agent_path = self.directory / "agent.wav"
        self.conversation_path = self.directory / "conversation.wav"
        self.manifest_path = self.directory / "manifest.json"
        self._queue: queue.Queue[_AudioItem | None] = queue.Queue(
            maxsize=self.config.queue_frames
        )
        self._dropped_frames = 0
        self._drop_lock = threading.Lock()
        self._closed = False
        self._close_lock = threading.Lock()
        self._worker_error: BaseException | None = None
        self._samples = {"remote": 0, "agent": 0}
        self._frames = {"remote": 0, "agent": 0}
        self._worker = threading.Thread(
            target=self._write_loop,
            name=f"call-recorder-{self.recording_id}",
            daemon=True,
        )
        self._worker.start()

    @classmethod
    def create_if_authorized(
        cls,
        *,
        call_id: str,
        caller_id: str,
        channel: str,
        sample_rate: int = 16_000,
        config: RecordingConfig | None = None,
    ) -> CallRecordingSession | None:
        effective = config or RecordingConfig.from_env()
        if not effective.enabled or not effective.consent_granted:
            return None
        return cls(
            call_id=call_id,
            caller_id=caller_id,
            channel=channel,
            sample_rate=sample_rate,
            config=effective,
        )

    def record_remote(self, pcm: bytes) -> None:
        self._enqueue("remote", pcm)

    def record_agent(self, pcm: bytes) -> None:
        self._enqueue("agent", pcm)

    def _enqueue(self, direction: Direction, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        # PCM16 must end on a sample boundary. A malformed partial sample is
        # discarded and makes the recording explicitly incomplete.
        if len(pcm) % 2:
            with self._drop_lock:
                self._dropped_frames += 1
            return
        try:
            self._queue.put_nowait(_AudioItem(direction, time.monotonic_ns(), bytes(pcm)))
        except queue.Full:
            with self._drop_lock:
                self._dropped_frames += 1

    def finalize(self, *, outcome: str) -> RecordingResult:
        with self._close_lock:
            if self._closed:
                raise RecordingError("recording is already finalized")
            self._closed = True
            while self._worker.is_alive():
                try:
                    self._queue.put(None, timeout=0.1)
                    break
                except queue.Full:
                    continue
            self._worker.join(timeout=30)
            if self._worker.is_alive():
                raise RecordingError("recording writer did not stop")
            if self._worker_error is not None:
                raise RecordingError("recording writer failed") from self._worker_error
            self._mix_tracks()
            dropped = self._dropped_frames
            complete = (
                dropped == 0
                and self._samples["remote"] > 0
                and self._samples["agent"] > 0
            )
            files = {
                path.name: {
                    "bytes": path.stat().st_size,
                    "sha256": self._sha256(path),
                }
                for path in (self.remote_path, self.agent_path, self.conversation_path)
            }
            manifest: dict[str, Any] = {
                "version": 1,
                "recording_id": self.recording_id,
                "channel": self.channel,
                "subject": self._subject,
                "consent": {"granted": True, "source": "per_call_operator_control"},
                "audio": {
                    "encoding": "pcm_s16le",
                    "sample_rate_hz": self.sample_rate,
                    "channels_per_track": 1,
                    "remote_samples": self._samples["remote"],
                    "agent_samples": self._samples["agent"],
                    "remote_frames": self._frames["remote"],
                    "agent_frames": self._frames["agent"],
                    "dropped_frames": dropped,
                },
                "outcome": str(outcome)[:80],
                "complete": complete,
                "started_unix": int(time.time() - (time.monotonic_ns() - self._started_ns) / 1e9),
                "ended_unix": int(time.time()),
                "files": files,
            }
            self._write_manifest(manifest)
            return RecordingResult(
                recording_id=self.recording_id,
                directory=self.directory,
                manifest=self.manifest_path,
                complete=complete,
                dropped_frames=dropped,
            )

    def _validate_root(self) -> None:
        root = self.config.root
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            raise RecordingError("recording root must be a non-symlink directory")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink():
            raise RecordingError("recording root must not be a symlink")
        os.chmod(root, 0o700)

    def _write_loop(self) -> None:
        writers: dict[Direction, wave.Wave_write] = {}
        try:
            for direction, path in (
                ("remote", self.remote_path),
                ("agent", self.agent_path),
            ):
                writer = wave.open(str(path), "wb")
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(self.sample_rate)
                writers[direction] = writer
            while True:
                item = self._queue.get()
                if item is None:
                    break
                writer = writers[item.direction]
                target_sample = max(
                    0,
                    int((item.captured_ns - self._started_ns) * self.sample_rate / 1_000_000_000),
                )
                current = self._samples[item.direction]
                if target_sample > current:
                    writer.writeframesraw(b"\0\0" * (target_sample - current))
                    current = target_sample
                writer.writeframesraw(item.pcm)
                self._samples[item.direction] = current + len(item.pcm) // 2
                self._frames[item.direction] += 1
        except BaseException as exc:
            self._worker_error = exc
        finally:
            for writer in writers.values():
                try:
                    writer.close()
                except Exception:
                    pass
            for path in (self.remote_path, self.agent_path):
                if path.exists():
                    os.chmod(path, 0o600)

    def _mix_tracks(self) -> None:
        with wave.open(str(self.remote_path), "rb") as remote, wave.open(
            str(self.agent_path), "rb"
        ) as agent, wave.open(str(self.conversation_path), "wb") as mixed:
            mixed.setnchannels(1)
            mixed.setsampwidth(2)
            mixed.setframerate(self.sample_rate)
            while True:
                remote_pcm = remote.readframes(4096)
                agent_pcm = agent.readframes(4096)
                if not remote_pcm and not agent_pcm:
                    break
                size = max(len(remote_pcm), len(agent_pcm))
                remote_pcm += b"\0" * (size - len(remote_pcm))
                agent_pcm += b"\0" * (size - len(agent_pcm))
                sample_count = size // 2
                left = struct.unpack(f"<{sample_count}h", remote_pcm)
                right = struct.unpack(f"<{sample_count}h", agent_pcm)
                output = [max(-32768, min(32767, a + b)) for a, b in zip(left, right, strict=True)]
                mixed.writeframesraw(struct.pack(f"<{sample_count}h", *output))
        os.chmod(self.conversation_path, 0o600)

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        temporary = self.directory / ".manifest.json.tmp"
        payload = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, payload.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, self.manifest_path)
        os.chmod(self.manifest_path, 0o600)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


def enforce_recording_retention(
    config: RecordingConfig | None = None, *, now: float | None = None
) -> int:
    """Delete only expired, direct child recording directories."""

    effective = config or RecordingConfig.from_env()
    root = effective.root
    if not root.exists():
        return 0
    if root.is_symlink() or not root.is_dir():
        raise RecordingError("recording root must be a non-symlink directory")
    cutoff = (time.time() if now is None else now) - effective.retention_days * 86_400
    removed = 0
    for child in root.iterdir():
        try:
            mode = child.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode) or child.stat().st_mtime >= cutoff:
            continue
        shutil.rmtree(child)
        removed += 1
    return removed
