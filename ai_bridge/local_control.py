"""Shared authentication for the loopback Studio and local MCP process."""

from __future__ import annotations

import json
import os
import secrets
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_CONTROL_TOKEN_PATH = Path.home() / ".config" / "phone-agent" / "control.token"


class LocalControlError(RuntimeError):
    pass


def control_token_path() -> Path:
    path = Path(
        os.getenv("PHONE_AGENT_CONTROL_TOKEN_FILE", "").strip()
        or DEFAULT_CONTROL_TOKEN_PATH
    ).expanduser()
    if not path.is_absolute():
        raise LocalControlError("PHONE_AGENT_CONTROL_TOKEN_FILE must be absolute")
    return path


def load_or_create_control_token(path: Path | None = None) -> str:
    target = path or control_token_path()
    parent_existed = target.parent.exists()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_mode = target.parent.lstat().st_mode
    if not stat.S_ISDIR(parent_mode) or stat.S_ISLNK(parent_mode):
        raise LocalControlError("control token parent must be a real directory")
    if parent_mode & 0o022:
        raise LocalControlError("control token parent must not be group/world writable")
    if not parent_existed:
        os.chmod(target.parent, 0o700)
    try:
        mode = target.lstat().st_mode
    except FileNotFoundError:
        token = secrets.token_urlsafe(32)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return load_or_create_control_token(target)
        try:
            os.write(fd, (token + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        return token
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        raise LocalControlError("control token must be a regular non-symlink file")
    if mode & 0o077:
        raise LocalControlError("control token permissions must be 0600")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
        try:
            opened_mode = os.fstat(fd).st_mode
            if not stat.S_ISREG(opened_mode):
                raise LocalControlError("control token must remain a regular file")
            token = os.read(fd, 257).decode("utf-8").strip()
        finally:
            os.close(fd)
    except OSError as exc:
        raise LocalControlError("control token could not be safely opened") from exc
    if len(token) < 32 or len(token) > 256:
        raise LocalControlError("control token is invalid")
    return token


def local_control_request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    url_root = (
        base_url
        or os.getenv("PHONE_AGENT_WEB_URL", "").strip()
        or "http://127.0.0.1:8090"
    ).rstrip("/")
    if not url_root.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise LocalControlError("local control URL must use loopback HTTP")
    body = None
    headers = {
        "Authorization": f"Bearer {load_or_create_control_token()}",
        "Accept": "application/json",
    }
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{url_root}{path}", data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8")).get("message")
        except Exception:
            message = None
        raise LocalControlError(str(message or f"local control returned HTTP {exc.code}")) from None
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise LocalControlError("PhoneAgent Studio is unavailable") from exc
    if not isinstance(result, dict):
        raise LocalControlError("local control returned an invalid response")
    return result
