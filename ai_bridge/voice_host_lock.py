"""Single-owner guard for the physical PhoneAgent gateway."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import TextIO


class VoiceHostBusyError(RuntimeError):
    """Another voice host already owns the phone gateway."""


class VoiceHostLock:
    """Hold an advisory file lock for one voice-host process."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: TextIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            stream.seek(0)
            owner = stream.read().strip() or "unknown"
            stream.close()
            raise VoiceHostBusyError(
                f"another PhoneAgent voice host is already running (pid {owner})"
            ) from exc
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()))
        stream.flush()
        self._stream = stream

    def release(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()

    def __enter__(self) -> VoiceHostLock:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()
