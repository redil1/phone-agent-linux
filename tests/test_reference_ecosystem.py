"""Tests for Reference Agents and Ecosystem (Milestone 18)."""

from __future__ import annotations

from phone_agent_gateway.ai_bridge.reference_ecosystem import get_reference_package


def test_reference_packages_compilation_and_signature() -> None:
    domains = ["iptv_sales", "appointment_booking", "customer_support"]
    for dom in domains:
        pkg = get_reference_package(dom)
        assert pkg.metadata.domain == dom
        assert pkg.verify_signature("production_reference_key") is True
        assert len(pkg.tasks) > 0
