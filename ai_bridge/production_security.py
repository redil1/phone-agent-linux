"""Production call policy, redaction, and tamper-evident audit events."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

POLICY_PATH = Path.home() / ".config" / "phone-agent" / "policy.json"
AUDIT_PATH = Path.home() / ".local" / "share" / "phone-agent" / "audit.jsonl"
E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
EMERGENCY = frozenset({"000", "08", "09", "15", "17", "18", "19", "110", "112", "911", "999"})


class PolicyError(ValueError):
    pass


def normalize_destination(value: str, country_code: str = "212") -> str:
    raw = re.sub(r"[\s().-]", "", str(value or ""))
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    elif raw.startswith("+"):
        pass
    elif raw.startswith("0") and raw[1:].isdigit():
        raw = f"+{country_code}{raw[1:]}"
    elif raw.isdigit():
        raw = "+" + raw
    if not E164_RE.fullmatch(raw):
        raise PolicyError("destination must be a valid E.164 telephone number")
    return raw


def public_destination(value: str, salt: str) -> str:
    digest = hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()[:16]
    return f"sha256:{digest}:last4:{value[-4:]}"


@dataclass(frozen=True, slots=True)
class DialDecision:
    allowed: bool
    reason: str
    normalized: str | None = None
    public_destination: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    dial_enabled: bool = True
    require_operator_approval: bool = True
    max_calls_per_hour: int = 20
    destination_cooldown_secs: int = 30
    max_call_duration_secs: int = 900
    allowlist: frozenset[str] = frozenset()
    denylist: frozenset[str] = frozenset()
    premium_prefixes: tuple[str, ...] = ()


def load_policy(path: Path | None = None) -> PolicyConfig:
    source = path or Path(os.getenv("PHONE_AGENT_POLICY_FILE", "").strip() or POLICY_PATH)
    if not source.exists():
        return PolicyConfig()
    if not source.is_file() or source.is_symlink():
        raise PolicyError("policy file must be a regular non-symlink file")
    metadata = source.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise PolicyError("policy file must be user-owned and not group/world writable")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError("policy file is invalid") from exc
    if not isinstance(data, dict):
        raise PolicyError("policy must be an object")
    allowed = {
        "version", "dial_enabled", "require_operator_approval", "max_calls_per_hour",
        "destination_cooldown_secs", "max_call_duration_secs", "allowlist", "denylist",
        "premium_prefixes",
    }
    if set(data) - allowed or data.get("version") != 1:
        raise PolicyError("policy version or fields are invalid")
    booleans = ("dial_enabled", "require_operator_approval")
    for name in booleans:
        if name in data and not isinstance(data[name], bool):
            raise PolicyError(f"policy {name} must be boolean")
    bounds = {
        "max_calls_per_hour": (1, 1_000),
        "destination_cooldown_secs": (0, 86_400),
        "max_call_duration_secs": (30, 3_600),
    }
    for name, (minimum, maximum) in bounds.items():
        if name in data and (
            not isinstance(data[name], int) or not minimum <= data[name] <= maximum
        ):
            raise PolicyError(f"policy {name} is out of range")
    lists: dict[str, tuple[str, ...]] = {}
    for name in ("allowlist", "denylist", "premium_prefixes"):
        value = data.get(name, [])
        if not isinstance(value, list) or len(value) > 1_000:
            raise PolicyError(f"policy {name} must be a bounded list")
        lists[name] = tuple(str(item) for item in value)
    for name in ("allowlist", "denylist"):
        if any(not E164_RE.fullmatch(value) for value in lists[name]):
            raise PolicyError(f"policy {name} contains a non-E.164 value")
    if any(not re.fullmatch(r"\+[1-9]\d{0,14}", value) for value in lists["premium_prefixes"]):
        raise PolicyError("policy premium_prefixes contains an invalid prefix")
    defaults = PolicyConfig()
    return PolicyConfig(
        dial_enabled=data.get("dial_enabled", defaults.dial_enabled),
        require_operator_approval=data.get(
            "require_operator_approval", defaults.require_operator_approval
        ),
        max_calls_per_hour=data.get("max_calls_per_hour", defaults.max_calls_per_hour),
        destination_cooldown_secs=data.get(
            "destination_cooldown_secs", defaults.destination_cooldown_secs
        ),
        max_call_duration_secs=data.get(
            "max_call_duration_secs", defaults.max_call_duration_secs
        ),
        allowlist=frozenset(lists["allowlist"]),
        denylist=frozenset(lists["denylist"]),
        premium_prefixes=lists["premium_prefixes"],
    )


class CallPolicy:
    def __init__(self, config: PolicyConfig | None = None, *, salt: str | None = None) -> None:
        self.config = config or load_policy()
        self.salt = salt or os.getenv("PHONE_AGENT_REDACTION_SALT", "phone-agent-local")
        self._calls: deque[float] = deque()
        self._last_destination: dict[str, float] = defaultdict(float)
        self._lock = threading.Lock()

    def decide_dial(
        self,
        destination: str,
        *,
        approved: bool,
        country_code: str = "212",
        now: float | None = None,
        reserve: bool = True,
    ) -> DialDecision:
        try:
            normalized = normalize_destination(destination, country_code)
        except PolicyError as exc:
            return DialDecision(False, str(exc))
        public = public_destination(normalized, self.salt)
        local_digits = normalized.lstrip("+")
        is_emergency = any(
            local_digits.endswith(code) and len(local_digits) <= len(code) + 3
            for code in EMERGENCY
        )
        if is_emergency:
            return DialDecision(False, "emergency destinations are forbidden", normalized, public)
        if not self.config.dial_enabled:
            return DialDecision(False, "dialing is disabled by policy", normalized, public)
        if self.config.require_operator_approval and not approved:
            return DialDecision(False, "operator approval is required", normalized, public)
        if normalized in self.config.denylist:
            return DialDecision(False, "destination is denied by policy", normalized, public)
        if self.config.allowlist and normalized not in self.config.allowlist:
            return DialDecision(False, "destination is not allowlisted", normalized, public)
        if any(normalized.startswith(prefix) for prefix in self.config.premium_prefixes):
            return DialDecision(False, "premium destinations are forbidden", normalized, public)
        current = time.monotonic() if now is None else now
        with self._lock:
            while self._calls and current - self._calls[0] >= 3_600:
                self._calls.popleft()
            if len(self._calls) >= self.config.max_calls_per_hour:
                return DialDecision(False, "hourly call rate exceeded", normalized, public)
            previous = self._last_destination[normalized]
            if previous and current - previous < self.config.destination_cooldown_secs:
                return DialDecision(False, "destination cooldown is active", normalized, public)
            if reserve:
                self._calls.append(current)
                self._last_destination[normalized] = current
        return DialDecision(True, "allowed", normalized, public)


class AuditLedger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(os.getenv("PHONE_AGENT_AUDIT_FILE", "").strip() or AUDIT_PATH)
        self._lock = threading.Lock()

    def append(self, event: str, details: dict[str, Any]) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", event):
            raise ValueError("audit event name is invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            # Reading the tail and appending the next record are one critical
            # section. Otherwise simultaneous callers could both extend the
            # same hash and silently fork the audit chain.
            previous = self._last_hash()
            body = {
                "version": 1,
                "timestamp": int(time.time()),
                "event": event,
                "details": self._safe(details),
                "previous_hash": previous,
            }
            canonical = json.dumps(
                body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            body["hash"] = hashlib.sha256(canonical.encode()).hexdigest()
            line = (
                json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                + "\n"
            )
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(self.path, 0o600)
        return body

    def _last_hash(self) -> str:
        if not self.path.is_file():
            return "0" * 64
        try:
            with self.path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                end = stream.tell()
                if end == 0:
                    return "0" * 64
                position = end - 1
                while position > 0:
                    stream.seek(position)
                    if stream.read(1) == b"\n" and position < end - 1:
                        break
                    position -= 1
                stream.seek(position + (1 if position else 0))
                return str(json.loads(stream.readline()).get("hash") or "0" * 64)
        except (OSError, json.JSONDecodeError):
            raise RuntimeError("audit ledger tail is invalid") from None

    @classmethod
    def _safe(cls, value: Any, *, depth: int = 0) -> Any:
        if depth > 6:
            return "<truncated>"
        if isinstance(value, dict):
            return {str(k)[:80]: cls._safe(v, depth=depth + 1) for k, v in list(value.items())[:64]}
        if isinstance(value, list):
            return [cls._safe(item, depth=depth + 1) for item in value[:64]]
        if isinstance(value, str):
            return value[:1_000]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[:500]
