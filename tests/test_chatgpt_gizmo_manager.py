"""Unit tests for ChatGPTGizmoManager."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from phone_agent_gateway.ai_bridge.chatgpt_gizmo_manager import ChatGPTGizmoManager


@pytest.fixture
def mock_auth():
    auth = MagicMock()
    auth.get_token.return_value = "dummy_token"
    return auth


def test_gizmo_manager_signature(mock_auth, tmp_path: Path):
    cache_file = tmp_path / "gizmo_cache.json"
    manager = ChatGPTGizmoManager(auth_manager=mock_auth, cache_path=cache_file)

    sig1 = manager.compute_signature("prompt 1", {"name": "Adam", "role": "Sales"}, "task_1")
    sig2 = manager.compute_signature("prompt 1", {"name": "Adam", "role": "Sales"}, "task_1")
    sig3 = manager.compute_signature("prompt 2", {"name": "Adam", "role": "Sales"}, "task_1")

    assert sig1 == sig2
    assert sig1 != sig3


@pytest.mark.asyncio
async def test_gizmo_manager_cached_retrieval(mock_auth, tmp_path: Path):
    cache_file = tmp_path / "gizmo_cache.json"
    manager = ChatGPTGizmoManager(auth_manager=mock_auth, cache_path=cache_file)

    sig = manager.compute_signature("prompt", {"name": "Adam"}, "task_iptv")
    manager._cache[sig] = "g-test-gizmo-12345"
    manager._save_cache()

    # Recreate manager to test disk load
    manager2 = ChatGPTGizmoManager(auth_manager=mock_auth, cache_path=cache_file)
    res = await manager2.get_or_create_gizmo(
        "prompt",
        {"identity": {"name": "Adam"}},
        {"id": "task_iptv", "title": "IPTV Sales"},
    )

    assert res == "g-test-gizmo-12345"
