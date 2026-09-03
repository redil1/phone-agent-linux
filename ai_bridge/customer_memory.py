"""Customer memory and privacy lifecycle management.

Governed by Milestone 11 (M11-01 through M11-10):
Separates memory classes, defines memory candidate schemas, keeps LLMs off
direct write paths, and supports GDPR-compliant customer export and deletion.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MemoryCandidate(StrictModel):
    caller_id: str
    key: str
    value: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    sensitivity: Literal["public", "private", "pii"] = "private"


class CustomerMemoryStore:
    """Scoped customer memory store with privacy erasure (M11-01, M11-07)."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, str]] = {}

    def commit_memory(self, candidate: MemoryCandidate) -> bool:
        """Deterministic policy validation before commit (M11-03)."""
        if candidate.confidence < 0.7:
            return False  # Reject low-confidence candidates
        if candidate.caller_id not in self.store:
            self.store[candidate.caller_id] = {}
        self.store[candidate.caller_id][candidate.key] = candidate.value
        return True

    def retrieve_memory(self, caller_id: str) -> dict[str, str]:
        return self.store.get(caller_id, {}).copy()

    def export_customer_data(self, caller_id: str) -> dict[str, str]:
        """GDPR/CCPA export (M11-07)."""
        return self.retrieve_memory(caller_id)

    def erase_customer_data(self, caller_id: str) -> bool:
        """GDPR/CCPA complete erasure (M11-07)."""
        if caller_id in self.store:
            del self.store[caller_id]
            return True
        return False
