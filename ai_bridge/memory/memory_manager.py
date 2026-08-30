"""Layered Memory Manager for PhoneAgent.

Manages persistent Core, Semantic, Episodic, and Working memory per phone number.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from ..secure_storage import atomic_write_private, harden_private_file

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_STORE = Path(
    os.getenv(
        "PHONE_AGENT_MEMORY_PATH",
        str(Path.home() / ".local" / "share" / "phone-agent" / "caller_memory_store.json"),
    )
).expanduser()


class LayeredMemoryManager:
    """Persistent caller memory store with semantic preferences and episodic call summaries."""

    def __init__(self, storage_path: Path | None = None) -> None:
        self.storage_path = storage_path or DEFAULT_MEMORY_STORE
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if self.storage_path.exists():
            try:
                harden_private_file(self.storage_path)
                with self.storage_path.open(encoding="utf-8") as stream:
                    loaded = json.load(stream)
                self._cache = loaded if isinstance(loaded, dict) else {}
            except Exception as exc:
                logger.warning("Could not read memory store: %s. Starting fresh.", exc)
                self._cache = {}

    def refresh(self) -> None:
        """Reload changes written by the separate live-call process."""
        with self._lock:
            self._load()

    def _save(self) -> None:
        try:
            payload = json.dumps(self._cache, ensure_ascii=False, indent=2) + "\n"
            atomic_write_private(self.storage_path, payload)
        except Exception as exc:
            logger.error("Failed to write caller memory store: %s", exc)

    @staticmethod
    def normalize_caller_id(phone_number: str) -> str:
        value = str(phone_number or "").strip()
        if value.startswith("unknown:"):
            return value
        leading_plus = value.startswith("+")
        digits = "".join(character for character in value if character.isdigit())
        return ("+" if leading_plus else "") + digits if digits else "anonymous"

    def get_caller_memory(self, phone_number: str) -> dict[str, Any]:
        """Retrieve semantic and episodic memory for a phone number."""
        caller_id = self.normalize_caller_id(phone_number)
        with self._lock:
            if caller_id not in self._cache:
                self._cache[caller_id] = {
                    "phone_number": caller_id,
                    "created_at": time.time(),
                    "call_count": 0,
                    "preferences": {},
                    "verified_facts": [],
                    "past_call_summary": "",
                    "episodic_turns": [],
                }
            return json.loads(json.dumps(self._cache[caller_id], ensure_ascii=False))

    def update_preferences(self, phone_number: str, preferences: dict[str, Any]) -> None:
        """Update verified semantic preferences."""
        caller_id = self.normalize_caller_id(phone_number)
        with self._lock:
            self._ensure_caller(caller_id)["preferences"].update(preferences)
            self._cache[caller_id]["updated_at"] = time.time()
            self._save()

    def update_identity(self, phone_number: str, *, name: str) -> None:
        caller_id = self.normalize_caller_id(phone_number)
        with self._lock:
            self._ensure_caller(caller_id)["name"] = name.strip()
            self._cache[caller_id]["updated_at"] = time.time()
            self._save()

    def record_turn(
        self,
        phone_number: str,
        *,
        caller_text: str,
        ai_response: str,
        turn_latency_ms: float = 0.0,
        fidelity_score: float = 100.0,
        task_id: str = "",
        evaluation_feedback: list[str] | None = None,
    ) -> None:
        """Record a completed conversation turn to episodic memory."""
        caller_id = self.normalize_caller_id(phone_number)
        with self._lock:
            mem = self._ensure_caller(caller_id)
            mem["episodic_turns"].append(
                {
                    "timestamp": time.time(),
                    "caller": caller_text,
                    "ai": ai_response,
                    "latency_ms": round(turn_latency_ms, 1),
                    "fidelity_score": round(fidelity_score, 1),
                    "task_id": task_id,
                    "evaluation_feedback": evaluation_feedback or [],
                }
            )
            mem["episodic_turns"] = mem["episodic_turns"][-50:]
            self._save()

    def complete_call_session(self, phone_number: str, summary: str = "") -> None:
        """Increment call counter and save session summary."""
        caller_id = self.normalize_caller_id(phone_number)
        with self._lock:
            mem = self._ensure_caller(caller_id)
            mem["call_count"] = mem.get("call_count", 0) + 1
            if summary:
                mem["past_call_summary"] = summary
            mem["last_call_at"] = time.time()
            self._save()

    def get_all_callers(self) -> list[dict[str, Any]]:
        """Return all caller memory records for dashboard inspection."""
        with self._lock:
            return json.loads(json.dumps(list(self._cache.values()), ensure_ascii=False))

    def get_recent_evaluations(self, limit: int = 20) -> list[dict[str, Any]]:
        evaluations: list[dict[str, Any]] = []
        with self._lock:
            for caller in self._cache.values():
                for turn in caller.get("episodic_turns", []):
                    evaluations.append(
                        {
                            "phone_number": caller.get("phone_number", ""),
                            "timestamp": turn.get("timestamp", 0),
                            "task_id": turn.get("task_id", ""),
                            "score": turn.get("fidelity_score", 0),
                            "feedback": turn.get("evaluation_feedback", []),
                        }
                    )
        evaluations.sort(key=lambda item: float(item.get("timestamp", 0)), reverse=True)
        return evaluations[:limit]

    def _ensure_caller(self, caller_id: str) -> dict[str, Any]:
        if caller_id not in self._cache:
            self._cache[caller_id] = {
                "phone_number": caller_id,
                "created_at": time.time(),
                "call_count": 0,
                "preferences": {},
                "verified_facts": [],
                "past_call_summary": "",
                "episodic_turns": [],
            }
        return self._cache[caller_id]
