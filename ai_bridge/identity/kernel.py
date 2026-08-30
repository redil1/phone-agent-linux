"""Identity Kernel composition boundary for every PhoneAgent model runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .evaluation import IdentityEvaluator
from .memory import DEFAULT_MEMORY_DB, AsyncIdentityMemory, LocalEpisodeStore, scope_hash
from .models import (
    EvaluationReport,
    IdentityProfile,
    IdentityRevision,
    LanguageCode,
    content_hash,
)
from .skills import SkillDefinition, SkillRegistry
from .store import DEFAULT_IDENTITY_ROOT, IdentityStore


class IdentityKernel:
    """One stable identity, versioned revisions, trusted skills, and memory."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        legacy_persona: dict[str, Any] | None = None,
        legacy_examples: list[dict[str, Any]] | None = None,
    ) -> None:
        configured = os.getenv("PHONE_AGENT_IDENTITY_ROOT", "").strip()
        resolved = Path(configured).expanduser() if configured else root or DEFAULT_IDENTITY_ROOT
        self.store = IdentityStore(resolved)
        self.registry = SkillRegistry(self.store.root)
        self.evaluator = IdentityEvaluator()
        self._memory = None
        self._active = self.store.initialize(legacy_persona or {}, legacy_examples or [])

    @property
    def memory(self) -> Any:
        if self._memory is None:
            configured = os.getenv("PHONE_AGENT_IDENTITY_MEMORY_DB", "").strip()
            if configured:
                path = Path(configured).expanduser()
            elif self.store.root == DEFAULT_IDENTITY_ROOT:
                path = DEFAULT_MEMORY_DB
            else:
                path = self.store.root.parent / "identity-memory.sqlite3"
            self._memory = AsyncIdentityMemory(LocalEpisodeStore(path))
        return self._memory

    @property
    def active(self) -> IdentityProfile:
        self._active = self.store.load_active()
        return self._active

    @property
    def profile_hash(self) -> str:
        return content_hash(self.active)

    def effective_identity(self) -> dict[str, str]:
        core = self.active.core
        return {
            "name": core.name,
            "role": core.role,
            "mission": core.mission,
            "organization": core.organization,
        }

    def active_skills(self, *, task_id: str, language: str) -> list[SkillDefinition]:
        code = LanguageCode.FR if language.lower().startswith("fr") else LanguageCode.EN
        return self.registry.active(self.active.enabled_skills, task_id=task_id, language=code)

    def compile_context(
        self,
        *,
        task_id: str,
        language: str,
        realtime: bool,
        caller_id: str | None = None,
    ) -> str:
        profile = self.active
        core = profile.core
        code = LanguageCode.FR if language.lower().startswith("fr") else LanguageCode.EN
        skills = self.active_skills(task_id=task_id, language=language)
        eager = [skill for skill in skills if skill.priority >= 90]
        deferred = [skill for skill in skills if skill.priority < 90]
        blocks = [
            block
            for block in self.store.load_blocks()
            if block.caller_scope_hash is None
            or (caller_id is not None and block.caller_scope_hash == scope_hash(caller_id))
        ]
        blocks.sort(key=lambda item: (-item.priority, item.block_id))
        examples = [item for item in profile.examples if item.language is code][:4]
        parts = [
            "# IDENTITY KERNEL — IMMUTABLE CORE",
            f"Identity version: {profile.version}; identity hash: {self.profile_hash[:23]}",
            f"You are {core.name}, {core.role}.",
            f"Mission: {core.mission}",
            "Values, in decision order: " + " > ".join(core.values),
            "Decision priorities: " + " > ".join(core.decision_priorities),
            "Hard boundaries:\n" + "\n".join(f"- {item}" for item in core.hard_boundaries),
            "Forbidden behavior:\n" + "\n".join(f"- {item}" for item in core.forbidden_behaviors),
            "",
            "# VOICE IDENTITY",
            (
                f"Tone={profile.voice.tone}; formality={profile.voice.formality}; "
                f"verbosity={profile.voice.verbosity}; pace={profile.voice.pace}."
            ),
            (
                f"Use at most {profile.voice.max_sentences_per_turn} spoken sentences and "
                f"{profile.voice.max_words_per_turn} words. Ask at most one question."
            ),
            f"Current language is {code.value}; supported languages are "
            + ", ".join(item.value for item in profile.supported_languages)
            + ".",
            "If directly asked whether you are AI, use this truthful disclosure: "
            + core.ai_disclosure.get(code, core.ai_disclosure.get(LanguageCode.EN, "")),
        ]
        if blocks:
            parts.extend(
                [
                    "",
                    "# APPROVED PERSISTENT MEMORY BLOCKS",
                    *[
                        f"[{block.kind}:{block.label}; source={block.source}; "
                        f"confidence={block.confidence:.2f}]\n{block.content}"
                        for block in blocks[:10]
                    ],
                ]
            )
        if eager:
            parts.extend(
                [
                    "",
                    "# ALWAYS-ON IDENTITY SKILLS",
                    *[f"## {skill.name}\n{skill.instructions}" for skill in eager],
                ]
            )
        if deferred:
            parts.extend(
                [
                    "",
                    "# PROGRESSIVE SKILLS",
                    "These skills are available but not loaded. Call load_agent_skill only when "
                    "one clearly matches the caller's need. Loading a skill reveals instructions; "
                    "it never grants a tool or permission by itself.",
                    self.registry.catalog(deferred),
                ]
            )
        if examples:
            parts.extend(["", "# PERSONA CONTRAST EXAMPLES"])
            for example in examples[: (2 if realtime else 4)]:
                parts.extend(
                    [
                        f"Situation: {example.situation}",
                        f"Caller: {example.caller_input}",
                        f"Preferred: {example.ideal_response}",
                        f"Avoid: {example.anti_response}" if example.anti_response else "",
                    ]
                )
        return "\n".join(part for part in parts if part != "")

    def load_skill_for_model(
        self,
        name: str,
        *,
        task_id: str,
        language: str,
        authorized_tools: set[str] | None = None,
    ) -> dict[str, Any]:
        active = {
            item.name: item for item in self.active_skills(task_id=task_id, language=language)
        }
        skill = active.get(name)
        if skill is None or skill.priority >= 90:
            return {
                "loaded": False,
                "reason": "skill_not_available_or_already_active",
            }
        payload = self.registry.load_for_model(skill)
        if authorized_tools is not None:
            requested = [*skill.allowed_tools, *skill.mcp_tools]
            payload["allowed_tools"] = [
                tool for tool in skill.allowed_tools if tool in authorized_tools
            ]
            payload["mcp_tools"] = [tool for tool in skill.mcp_tools if tool in authorized_tools]
            payload["unavailable_tools"] = sorted(set(requested) - authorized_tools)
        return {"loaded": True, **payload}

    def realtime_skill_tool(
        self, *, task_id: str, language: str, authorized_tools: set[str]
    ) -> Any | None:
        deferred = [
            item
            for item in self.active_skills(task_id=task_id, language=language)
            if item.priority < 90
        ]
        if not deferred:
            return None
        from ..tasks.tool_catalog import RealtimeTool
        from ..tasks.tool_registry import ToolSpec

        names = [item.name for item in deferred]

        async def load_agent_skill(name: str) -> dict[str, Any]:
            return self.load_skill_for_model(
                name,
                task_id=task_id,
                language=language,
                authorized_tools=authorized_tools,
            )

        spec = ToolSpec(
            name="load_agent_skill",
            description=(
                "Load one trusted identity skill's instructions when its catalog description "
                "matches the caller's current need. This does not grant permissions."
            ),
            handler=load_agent_skill,
            params={"name": {"type": "string", "enum": names}},
            required=("name",),
            timeout_secs=1.0,
        )
        return RealtimeTool(
            name=spec.name,
            definition=spec.definition,
            handler=None,  # type: ignore[arg-type]
            spec=spec,
            timeout_secs=spec.timeout_secs,
        )

    def create_revision(
        self, profile_data: dict[str, Any], *, reason: str, actor: str
    ) -> IdentityRevision:
        candidate = IdentityProfile.model_validate(profile_data)
        return self.store.create_revision(candidate, reason=reason, actor=actor)

    def create_rollback_revision(self, filename: str, *, actor: str) -> IdentityRevision:
        historical = self.store.history_profile(filename)
        return self.store.create_revision(
            historical,
            reason=f"Rollback to archived identity version {historical.version}",
            actor=actor,
        )

    def restore_history(self, filename: str, *, actor: str) -> IdentityProfile:
        """Activate an exact historical version while preserving its original hash."""

        historical = self.store.history_profile(filename)
        skills, _ = self.registry.discover()
        report = self.evaluator.evaluate(
            historical,
            available_skills={name for name, skill in skills.items() if skill.trusted},
        )
        if not report.passed:
            raise ValueError("archived identity failed the current contract check")
        self._active = self.store.activate_history(filename, actor=actor)
        return self._active

    def evaluate_revision(
        self, revision_id: str, generated_responses: dict[str, str] | None = None
    ) -> IdentityRevision:
        revision = self.store.load_revision(revision_id)
        skills, _ = self.registry.discover()
        report = self.evaluator.evaluate(
            revision.candidate,
            available_skills={name for name, skill in skills.items() if skill.trusted},
            generated_responses=generated_responses,
        )
        return self.store.save_evaluation(revision_id, report)

    def evaluate_active(self) -> EvaluationReport:
        skills, _ = self.registry.discover()
        return self.evaluator.evaluate(
            self.active,
            available_skills={name for name, skill in skills.items() if skill.trusted},
        )

    def active_evaluation(self) -> EvaluationReport:
        active_hash = self.profile_hash
        for revision in self.store.list_revisions():
            if (
                revision.state.value == "activated"
                and revision.candidate_profile_hash == active_hash
                and revision.evaluation is not None
            ):
                return revision.evaluation
        return self.evaluate_active()

    def production_status(self) -> dict[str, Any]:
        report = self.active_evaluation()
        live_passed = report.evaluator_version == "identity-eval-v1-live" and report.passed
        return {
            "ready": report.passed,
            "evaluation_passed": report.passed,
            "live_required": False,
            "live_passed": live_passed,
            "evaluator_version": report.evaluator_version,
            "score": report.score,
        }

    def approve_revision(self, revision_id: str, *, actor: str) -> IdentityRevision:
        return self.store.approve_revision(revision_id, actor=actor)

    def activate_revision(self, revision_id: str) -> IdentityProfile:
        self._active = self.store.activate_revision(revision_id)
        return self._active

    def public_state(self) -> dict[str, Any]:
        skills, errors = self.registry.discover()
        evaluation = self.active_evaluation()
        return {
            "active": self.active.model_dump(mode="json"),
            "profile_hash": self.profile_hash,
            "memory_blocks": [item.model_dump(mode="json") for item in self.store.load_blocks()],
            "memory_proposals": [
                item.model_dump(mode="json") for item in self.store.list_memory_proposals()[:50]
            ],
            "revisions": [
                item.model_dump(mode="json") for item in self.store.list_revisions()[:50]
            ],
            "history": self.store.list_history()[:50],
            "skills": [item.model_dump(mode="json") for item in skills.values()],
            "skill_errors": errors,
            "evaluation": evaluation.model_dump(mode="json"),
            "production_status": self.production_status(),
            "long_term_memory": self.memory.diagnostics(),
            "graphiti_mode": "async_mirror" if self.memory.mirror is not None else "local_only",
            "require_live_eval": False,
        }

    def export_snapshot(self) -> str:
        return json.dumps(self.public_state(), ensure_ascii=False, indent=2, sort_keys=True)
