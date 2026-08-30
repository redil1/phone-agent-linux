"""Small private-file primitives for local configuration and caller data."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path


class SecureStorageError(OSError):
    pass


def ensure_private_parent(path: Path) -> None:
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = parent.lstat().st_mode
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        raise SecureStorageError("private storage parent must be a real directory")
    if parent.stat().st_uid != os.getuid():
        raise SecureStorageError("private storage parent must be owned by this user")
    os.chmod(parent, 0o700)


def harden_private_file(path: Path) -> None:
    if not path.exists():
        return
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        raise SecureStorageError("private storage file must be a regular non-symlink file")
    if path.stat().st_uid != os.getuid():
        raise SecureStorageError("private storage file must be owned by this user")
    os.chmod(path, 0o600)


def atomic_write_private(path: Path, payload: str | bytes) -> None:
    ensure_private_parent(path)
    if path.exists():
        harden_private_file(path)
    encoded = payload.encode("utf-8") if isinstance(payload, str) else payload
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        offset = 0
        while offset < len(encoded):
            offset += os.write(fd, encoded[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def append_private_line(path: Path, line: str) -> None:
    ensure_private_parent(path)
    if path.exists():
        harden_private_file(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
