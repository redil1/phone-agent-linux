"""Tests for Universal Agent Package V1 (Milestone 3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from phone_agent_gateway.ai_bridge.agent_package_v1 import (
    AgentMetadataV1,
    AgentPackageV1,
    ImmutableIdentityV1,
    UniversalTaskContractV1,
    VoiceProfileV1,
)


def _make_package(package_id: str, domain: str, role: str) -> AgentPackageV1:
    return AgentPackageV1(
        metadata=AgentMetadataV1(package_id=package_id, name=f"{role} Agent", domain=domain),
        identity=ImmutableIdentityV1(
            name=f"{role} Assistant",
            role=role,
            mission=f"Fulfill {domain} requests professionally.",
            forbidden_behavior=["Never disclose internal credentials."],
        ),
        tasks=[
            UniversalTaskContractV1(
                task_id=f"{domain}_primary",
                title=f"{role} Task",
                objective=f"Serve {domain} requests.",
            )
        ],
        voice=VoiceProfileV1(voice_id="M1"),
    )


def test_five_reference_agents_compile_without_changes() -> None:
    """Milestone 3 exit gate: 5 distinct domains compile cleanly."""
    domains = [
        ("sales_agent", "sales", "Sales Specialist"),
        ("support_agent", "support", "Customer Support"),
        ("booking_agent", "booking", "Appointment Scheduler"),
        ("triage_agent", "triage", "Technical Triage"),
        ("receptionist_agent", "routing", "Receptionist & Call Router"),
    ]
    packages = [_make_package(pid, dom, role) for pid, dom, role in domains]
    assert len(packages) == 5
    for pkg in packages:
        digest = pkg.compute_digest()
        assert len(digest) == 64
        pkg.sign("test_key")
        assert pkg.verify_signature("test_key") is True


def test_invalid_package_fails_validation() -> None:
    with pytest.raises(ValidationError):
        # Invalid package_id regex
        _make_package("INVALID_UPPERCASE_ID", "test", "Test")


def test_package_export_import_roundtrip() -> None:
    original = _make_package("roundtrip_test", "sales", "Sales Rep")
    original.sign("secret_1")
    serialized = original.model_dump_json()

    restored = AgentPackageV1.model_validate_json(serialized)
    assert restored.compute_digest() == original.compute_digest()
    assert restored.signature == original.signature
    assert restored.verify_signature("secret_1") is True
