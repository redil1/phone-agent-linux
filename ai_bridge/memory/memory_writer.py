"""Validated Memory Writer for PhoneAgent.

Asynchronously analyzes completed speech turns, extracts verified facts/preferences,
and writes them to long-term caller memory without blocking real-time voice latency.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from ..identity.memory import AsyncIdentityMemory, LocalEpisodeStore, scope_hash
from ..identity.models import MemoryBlock, MemorySource
from ..identity.store import DEFAULT_IDENTITY_ROOT, IdentityStore
from .memory_manager import LayeredMemoryManager

logger = logging.getLogger(__name__)


class ValidatedMemoryWriter:
    """Extracts verified preferences and lessons from turns and commits them to memory."""

    def __init__(
        self,
        memory_manager: LayeredMemoryManager | None = None,
        identity_memory: AsyncIdentityMemory | None = None,
    ) -> None:
        self.memory_manager = memory_manager or LayeredMemoryManager()
        self._identity_memory = identity_memory

    def _long_term_memory(self) -> AsyncIdentityMemory:
        if self._identity_memory is None:
            configured = os.getenv("PHONE_AGENT_IDENTITY_MEMORY_DB", "").strip()
            path = (
                Path(configured).expanduser()
                if configured
                else self.memory_manager.storage_path.with_name("identity-memory.sqlite3")
            )
            self._identity_memory = AsyncIdentityMemory(LocalEpisodeStore(path))
        return self._identity_memory

    async def process_turn_async(
        self,
        phone_number: str,
        caller_text: str,
        ai_response: str,
        turn_latency_ms: float = 0.0,
        fidelity_score: float = 100.0,
        task_id: str = "",
        evaluation_feedback: list[str] | None = None,
    ) -> None:
        """Process one turn off the real-time event loop."""

        await asyncio.to_thread(
            self._process_turn,
            phone_number,
            caller_text,
            ai_response,
            turn_latency_ms,
            fidelity_score,
            task_id,
            evaluation_feedback or [],
        )

    def _process_turn(
        self,
        phone_number: str,
        caller_text: str,
        ai_response: str,
        turn_latency_ms: float,
        fidelity_score: float,
        task_id: str,
        evaluation_feedback: list[str],
    ) -> None:
        try:
            if re.search(r"[\u0600-\u06ff]", caller_text + ai_response):
                logger.info(
                    "Skipped memory write for %s because the turn is outside the "
                    "English/French policy",
                    phone_number,
                )
                return

            # 1. Record episodic turn immediately
            self.memory_manager.record_turn(
                phone_number,
                caller_text=caller_text,
                ai_response=ai_response,
                turn_latency_ms=turn_latency_ms,
                fidelity_score=fidelity_score,
                task_id=task_id,
                evaluation_feedback=evaluation_feedback,
            )
            memory = self._long_term_memory()
            call_reference = f"{task_id}:{int(time.time())}"
            language = "fr" if re.search(r"[àâçéèêëîïôùûüÿœ]", caller_text.lower()) else "en"
            memory.submit_turn(
                caller_id=phone_number,
                call_id=call_reference,
                role="caller",
                content=caller_text,
                language=language,
                task_id=task_id,
            )
            memory.submit_turn(
                caller_id=phone_number,
                call_id=call_reference,
                role="agent",
                content=ai_response,
                language=language,
                task_id=task_id,
            )

            # 2. Extract explicit language preferences
            extracted_prefs: dict[str, Any] = {}
            if re.search(r"\b(français|french)\b", caller_text, re.IGNORECASE):
                extracted_prefs["preferred_language"] = "fr-FR"
            elif re.search(r"\b(english|anglais)\b", caller_text, re.IGNORECASE):
                extracted_prefs["preferred_language"] = "en-US"

            # 3. Extract a caller name only when it is explicitly stated.
            name_match = re.search(
                r"(?:je m'appelle|my name is)\s+([A-Za-zÀ-ÖØ-öø-ÿ'-]{2,30})",
                caller_text,
                re.IGNORECASE,
            )
            if name_match:
                self.memory_manager.update_identity(
                    phone_number,
                    name=name_match.group(1).strip(),
                )

            if extracted_prefs:
                self.memory_manager.update_preferences(phone_number, extracted_prefs)
                logger.info(
                    "Committed validated preferences for %s: %s", phone_number, extracted_prefs
                )
                if os.getenv("PHONE_AGENT_IDENTITY_PROPOSALS_ENABLED", "false").lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }:
                    root = Path(
                        os.getenv("PHONE_AGENT_IDENTITY_ROOT", "").strip() or DEFAULT_IDENTITY_ROOT
                    ).expanduser()
                    language = str(extracted_prefs["preferred_language"])
                    IdentityStore(root).create_memory_proposal(
                        MemoryBlock(
                            block_id=f"caller_language_{scope_hash(phone_number)[7:23]}",
                            kind="human",
                            label="Caller language preference",
                            content=f"The caller explicitly prefers {language}.",
                            mutable=True,
                            priority=75,
                            source=MemorySource.AGENT_INFERRED,
                            confidence=1.0,
                            caller_scope_hash=scope_hash(phone_number),
                        ),
                        evidence=f"Explicit caller statement: {caller_text[:500]}",
                    )

        except Exception as exc:
            logger.error("Error in background memory writer: %s", exc)
