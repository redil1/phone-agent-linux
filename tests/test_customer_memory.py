"""Tests for Customer Memory and Continuity (Milestone 11)."""

from __future__ import annotations

from phone_agent_gateway.ai_bridge.customer_memory import (
    CustomerMemoryStore,
    MemoryCandidate,
)


def test_customer_memory_commit_and_privacy_lifecycle() -> None:
    store = CustomerMemoryStore()
    caller = "+15551234567"

    # Reject low confidence candidate
    low_cand = MemoryCandidate(caller_id=caller, key="preference", value="tea", confidence=0.5)
    assert store.commit_memory(low_cand) is False

    # Accept valid candidate
    good_cand = MemoryCandidate(caller_id=caller, key="preference", value="coffee", confidence=0.95)
    assert store.commit_memory(good_cand) is True

    # Retrieve & Export
    exported = store.export_customer_data(caller)
    assert exported == {"preference": "coffee"}

    # Erasure
    assert store.erase_customer_data(caller) is True
    assert store.retrieve_memory(caller) == {}
