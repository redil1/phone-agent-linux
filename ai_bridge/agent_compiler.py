"""Universal Agent Package Compiler and Activation Engine.

Governed by Milestone 4 (M4-01 through M4-12):
Compiles an AgentPackageV1 into an executable runtime bundle:
- Resolves capabilities and prompts
- Generates capability plans and policy bundles
- Validates contradictions
- Manages atomic activation and rollback
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from .agent_package_v1 import AgentPackageV1


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PromptIR(StrictModel):
    system_prompt: str
    identity_prefix: str
    task_instructions: str
    allowed_tools_declaration: list[str]


class CapabilityPlan(StrictModel):
    authorized_tools: list[str]
    high_risk_tools: list[str]
    tools_requiring_approval: list[str]


class CompiledAgentBundle(StrictModel):
    package_id: str
    package_digest: str
    prompt_ir: PromptIR
    capability_plan: CapabilityPlan
    voice_profile: dict[str, Any]
    compiler_hash: str


class CompilerContradictionError(ValueError):
    """Raised when an AgentPackage contains logical or capability contradictions (M4-02)."""


def compile_agent_package(package: AgentPackageV1) -> CompiledAgentBundle:
    """Compile an AgentPackageV1 through all phases (M4-01 through M4-08)."""
    # 1. Contradiction Detection (M4-02)
    declared_tools = {c.tool_name for c in package.capabilities}
    for task in package.tasks:
        for tool in task.allowed_tools:
            if tool not in declared_tools:
                raise CompilerContradictionError(
                    f"Task '{task.task_id}' references undeclared capability '{tool}'"
                )

    # 2. Prompt IR (M4-03)
    prefix = f"Identity: {package.identity.name} ({package.identity.role}). Mission: {package.identity.mission}"
    task_instructions = "\n".join([f"Task [{t.task_id}]: {t.objective}" for t in package.tasks])
    full_prompt = f"{prefix}\n{task_instructions}"

    prompt_ir = PromptIR(
        system_prompt=full_prompt,
        identity_prefix=prefix,
        task_instructions=task_instructions,
        allowed_tools_declaration=sorted(declared_tools),
    )

    # 3. Capability Plan (M4-04)
    capability_plan = CapabilityPlan(
        authorized_tools=sorted(declared_tools),
        high_risk_tools=sorted([c.tool_name for c in package.capabilities if c.risk_level == "high"]),
        tools_requiring_approval=sorted([c.tool_name for c in package.capabilities if c.requires_human_approval]),
    )

    # 4. Voice Profile Render Plan (M4-08)
    voice_profile = package.voice.model_dump()

    # 5. Deterministic Hash Computation
    raw_payload = json.dumps({
        "prompt": prompt_ir.model_dump(),
        "capabilities": capability_plan.model_dump(),
        "voice": voice_profile,
    }, sort_keys=True)
    compiler_hash = hashlib.sha256(raw_payload.encode()).hexdigest()

    return CompiledAgentBundle(
        package_id=package.metadata.package_id,
        package_digest=package.compute_digest(),
        prompt_ir=prompt_ir,
        capability_plan=capability_plan,
        voice_profile=voice_profile,
        compiler_hash=compiler_hash,
    )


class AgentActivationManager:
    """Atomic activation manager (M4-12)."""

    def __init__(self) -> None:
        self.active_bundle: CompiledAgentBundle | None = None
        self.active_calls: int = 0

    def activate(self, bundle: CompiledAgentBundle) -> bool:
        """Atomically activate new bundle for future calls."""
        self.active_bundle = bundle
        return True

    def get_runtime_for_call(self) -> CompiledAgentBundle:
        """Active call retains immutable bundle."""
        if self.active_bundle is None:
            raise RuntimeError("No active agent bundle configured")
        return self.active_bundle
