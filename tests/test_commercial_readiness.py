"""Tests for Commercial Readiness (Milestone 18-09/10 & Milestone 19)."""

from __future__ import annotations

from phone_agent_gateway.ai_bridge.commercial_readiness import CommercialReadinessManager


def test_commercial_metering_and_usage_tracking() -> None:
    mgr = CommercialReadinessManager()
    mgr.record_usage("tenant_xyz", minutes=12.5, tokens=4500, tools=3)

    usage = mgr.get_billable_usage("tenant_xyz")
    assert usage.call_minutes == 12.5
    assert usage.tokens_consumed == 4500
    assert usage.tools_dispatched == 3
    assert usage.edition == "managed_cloud"
