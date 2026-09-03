"""Tests for Agent Package Compiler and Activation (Milestone 4)."""

from __future__ import annotations

import pytest

from phone_agent_gateway.ai_bridge.agent_compiler import (
    AgentActivationManager,
    CompilerContradictionError,
    compile_agent_package,
)
from phone_agent_gateway.ai_bridge.agent_package_v1 import (
    AgentMetadataV1,
    AgentPackageV1,
    CapabilityManifestV1,
    ImmutableIdentityV1,
    UniversalTaskContractV1,
    VoiceProfileV1,
)


def _make_valid_package() -> AgentPackageV1:
    return AgentPackageV1(
        metadata=AgentMetadataV1(package_id="compiler_test", name="Compiler Test Agent"),
        identity=ImmutableIdentityV1(
            name="TestBot",
            role="Assistant",
            mission="Assist callers efficiently.",
        ),
        tasks=[
            UniversalTaskContractV1(
                task_id="main_task",
                title="Main Task",
                objective="Answer questions.",
                allowed_tools=["lookup_info"],
            )
        ],
        capabilities=[
            CapabilityManifestV1(
                tool_name="lookup_info",
                description="Look up factual info.",
                risk_level="low",
            ),
            CapabilityManifestV1(
                tool_name="place_order",
                description="Charge credit card.",
                risk_level="high",
                requires_human_approval=True,
            ),
        ],
        voice=VoiceProfileV1(voice_id="M1"),
    )


def test_compiler_deterministic_output() -> None:
    pkg = _make_valid_package()
    bundle1 = compile_agent_package(pkg)
    bundle2 = compile_agent_package(pkg)
    assert bundle1.compiler_hash == bundle2.compiler_hash
    assert bundle1.package_digest == bundle2.package_digest


def test_compiler_detects_contradictions() -> None:
    pkg = _make_valid_package()
    # Task references undeclared tool 'undeclared_tool'
    pkg.tasks[0].allowed_tools.append("undeclared_tool")
    with pytest.raises(CompilerContradictionError, match="references undeclared capability"):
        compile_agent_package(pkg)


def test_atomic_activation_and_call_immutability() -> None:
    mgr = AgentActivationManager()
    bundle = compile_agent_package(_make_valid_package())
    assert mgr.activate(bundle) is True
    active = mgr.get_runtime_for_call()
    assert active.package_id == "compiler_test"
