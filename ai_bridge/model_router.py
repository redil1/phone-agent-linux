"""LLM routing, context bounds, latency, and quality.

Governed by Milestone 7 (M7-01 through M7-14):
Standardizes LLM adapter contract, model capability registry, bounded context layout,
and latency-optimized speculative routing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ModelProfile(StrictModel):
    provider: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    context_window: int = Field(default=8192, ge=1024)
    streaming_supported: bool = True
    tool_calling_supported: bool = True
    tier: Literal["local_fast", "cloud_premium"] = "cloud_premium"


class ModelRegistry:
    """Model capability registry (M7-02)."""

    def __init__(self) -> None:
        self.registry: dict[str, ModelProfile] = {
            "gemini-flash": ModelProfile(
                provider="antigravity_gemini",
                model_name="gemini-3.1-flash-lite",
                context_window=32_000,
                tier="cloud_premium",
            ),
            "ollama-local": ModelProfile(
                provider="ollama",
                model_name="qwen2.5:3b",
                context_window=8192,
                tier="local_fast",
            ),
        }

    def get_profile(self, key: str) -> ModelProfile:
        if key not in self.registry:
            raise KeyError(f"Unknown model profile: {key}")
        return self.registry[key]


class StableContextLayout(StrictModel):
    """Stable byte prefix and bounded tail context layout (M7-04, M7-05)."""

    immutable_system_prefix: str
    verified_knowledge: list[str] = Field(default_factory=list)
    recent_dialogue_turns: list[dict[str, str]] = Field(default_factory=list)
    max_recent_turns: int = Field(default=8, ge=2, le=30)

    def add_turn(self, role: str, content: str) -> None:
        self.recent_dialogue_turns.append({"role": role, "content": content})
        if len(self.recent_dialogue_turns) > self.max_recent_turns:
            self.recent_dialogue_turns.pop(0)

    def render_prompt_bundle(self) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": self.immutable_system_prefix}]
        if self.verified_knowledge:
            facts = "\n".join(f"- {f}" for f in self.verified_knowledge)
            messages.append({"role": "system", "content": f"Verified Knowledge:\n{facts}"})
        messages.extend(self.recent_dialogue_turns)
        return messages
