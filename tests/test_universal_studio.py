"""Tests for Universal Agent Studio (Milestone 15)."""

from __future__ import annotations

from phone_agent_gateway.ai_bridge.universal_studio import (
    StudioLiveCallView,
    UniversalStudioManager,
)


def test_studio_deployment_and_live_operations() -> None:
    mgr = UniversalStudioManager()
    mgr.deploy_package("sales_v1", {"status": "active"})

    mgr.register_call(
        StudioLiveCallView(
            call_id="call_1",
            channel="gsm",
            stage="in_progress",
            latency_p50_ms=420.0,
            active_agent_id="sales_v1",
        )
    )

    overview = mgr.get_live_overview()
    assert overview["total_active_calls"] == 1
    assert "sales_v1" in overview["deployed_packages"]
    assert overview["calls"][0]["latency_p50_ms"] == 420.0
