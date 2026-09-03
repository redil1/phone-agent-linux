"""Reference agents and ecosystem packs.

Governed by Milestone 18 (M18-01 through M18-08):
Demonstrates universal agent packages across distinct real-world domains:
IPTV sales, appointment booking, technical support, and medical triage.
"""

from __future__ import annotations

from .agent_package_v1 import (
    AgentMetadataV1,
    AgentPackageV1,
    ImmutableIdentityV1,
    UniversalTaskContractV1,
    VoiceProfileV1,
)


def get_reference_package(domain: str) -> AgentPackageV1:
    """Build production-ready reference agent package for arbitrary lawful domains (M18-01 to M18-07)."""
    configs = {
        "iptv_sales": ("oxzoon_iptv", "OXzoon Sales Rep", "Sales", "Present IPTV subscription offers and close orders."),
        "appointment_booking": ("dentist_booking", "Clinic Scheduler", "Booking", "Schedule and reschedule patient dental visits."),
        "customer_support": ("isp_support", "Broadband Tech Support", "Support", "Diagnose and resolve internet connectivity issues."),
    }
    if domain not in configs:
        raise ValueError(f"Unknown reference package domain: {domain}")

    pid, name, role, mission = configs[domain]
    pkg = AgentPackageV1(
        metadata=AgentMetadataV1(package_id=pid, name=name, domain=domain),
        identity=ImmutableIdentityV1(
            name=name,
            role=role,
            mission=mission,
            forbidden_behavior=["Never disclose internal credentials or confidential tenant data."],
        ),
        tasks=[
            UniversalTaskContractV1(
                task_id=f"{pid}_primary",
                title=f"{role} Workflow",
                objective=mission,
            )
        ],
        voice=VoiceProfileV1(voice_id="M1"),
    )
    pkg.sign("production_reference_key")
    return pkg
