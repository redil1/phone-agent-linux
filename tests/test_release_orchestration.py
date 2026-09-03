"""Tests for Release Orchestration and Worker Lifecycle (Milestone 17)."""

from __future__ import annotations

import pytest

from phone_agent_gateway.ai_bridge.release_orchestration import WorkerOrchestrationCluster


def test_worker_cluster_lifecycle_and_drain() -> None:
    cluster = WorkerOrchestrationCluster()
    cluster.register_worker("w1", "127.0.0.1", max_concurrency=2)

    # Assign calls
    assigned_1 = cluster.assign_call_to_worker()
    assert assigned_1 == "w1"
    assert cluster.workers["w1"].active_calls == 1

    # Drain worker (e.g. for upgrade)
    cluster.drain_worker("w1")
    assert cluster.workers["w1"].status == "draining"

    # Draining worker rejects new calls
    with pytest.raises(RuntimeError, match="No available healthy capacity"):
        cluster.assign_call_to_worker()

    # Active call finishes cleanly
    cluster.release_call("w1")
    assert cluster.workers["w1"].active_calls == 0
