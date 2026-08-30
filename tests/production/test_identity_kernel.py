from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from phone_agent_gateway.ai_bridge.identity.evaluation import IdentityEvaluator
from phone_agent_gateway.ai_bridge.identity.kernel import IdentityKernel
from phone_agent_gateway.ai_bridge.identity.live_eval import (
    OpenAIRealtimeIdentityEvaluator,
    profile_eval_instructions,
)
from phone_agent_gateway.ai_bridge.identity.memory import (
    AsyncIdentityMemory,
    GraphitiHttpMirror,
    IdentityMemoryError,
    LocalEpisodeStore,
    MemoryEpisode,
    scope_hash,
)
from phone_agent_gateway.ai_bridge.identity.models import (
    MemoryBlock,
    MemorySource,
)
from phone_agent_gateway.ai_bridge.identity.skills import SkillDraft, SkillRegistry
from phone_agent_gateway.ai_bridge.identity.store import IdentityStoreError
from phone_agent_gateway.ai_bridge.memory.memory_manager import LayeredMemoryManager
from phone_agent_gateway.ai_bridge.memory.memory_writer import ValidatedMemoryWriter
from phone_agent_gateway.ai_bridge.tasks.tool_catalog import execute_tool


def _legacy() -> tuple[dict, list[dict]]:
    persona = {
        "identity": {
            "name": "Adam",
            "role": "AI phone representative",
            "mission": (
                "Help callers make accurate decisions through a natural and respectful "
                "conversation."
            ),
        },
        "core_values": ["truth", "respect", "usefulness"],
        "decision_priority": ["factual_correctness", "caller_respect"],
        "hard_boundaries": [
            "Never invent facts or consent.",
            "Never claim an action succeeded without a verified tool result.",
            "Never make a commitment without authorization.",
        ],
        "communication": {
            "prohibited": [
                "Do not repeat the opening.",
                "Do not use robotic filler.",
                "Do not continue after refusal.",
            ]
        },
    }
    examples = [
        {
            "situation": "Caller asks who is speaking.",
            "context": {"language": "en-US"},
            "target_response": "It's Adam. I'm calling to help with your request.",
            "bad_response": "I am a generic assistant.",
            "why_bad": "It loses identity.",
        },
        {
            "situation": "Caller gives permission to continue.",
            "context": {"language": "en-US"},
            "target_response": "Thanks. What matters most to you?",
            "bad_response": "Excellent question.",
            "why_bad": "It sounds robotic.",
        },
        {
            "situation": "Caller continues in French.",
            "context": {"language": "fr-FR"},
            "target_response": "Bien sûr, je continue en français.",
            "bad_response": "I only speak English.",
            "why_bad": "It ignores language.",
        },
        {
            "situation": "Caller asks a question in French.",
            "context": {"language": "fr-FR"},
            "target_response": "Je vous écoute. Quel est le point principal ?",
            "bad_response": "How may I assist you?",
            "why_bad": "It switches language.",
        },
    ]
    return persona, examples


def _kernel(tmp_path: Path) -> IdentityKernel:
    persona, examples = _legacy()
    return IdentityKernel(
        root=tmp_path / "identity", legacy_persona=persona, legacy_examples=examples
    )


def test_identity_initialization_is_private_and_compiles_both_languages(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    assert kernel.active.core.name == "Adam"
    assert kernel.evaluate_active().passed
    prompt = kernel.compile_context(task_id="customer_support", language="fr-FR", realtime=True)
    assert "IDENTITY KERNEL" in prompt
    assert "Adam" in prompt
    assert "Phone Conversation" in prompt
    assert "Je suis Adam" in prompt
    assert os.stat(kernel.store.active_path).st_mode & 0o777 == 0o600
    assert os.stat(kernel.store.root).st_mode & 0o777 == 0o700


def test_revision_requires_evaluation_exact_approval_and_activation(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    candidate = kernel.active.model_copy(deep=True)
    candidate.core.mission = (
        "Help every caller make a truthful decision while remaining concise, warm, and safe."
    )
    revision = kernel.create_revision(
        candidate.model_dump(mode="json"), reason="Clarify the mission", actor="operator"
    )
    with pytest.raises(IdentityStoreError, match="approved"):
        kernel.activate_revision(revision.revision_id)
    evaluated = kernel.evaluate_revision(revision.revision_id)
    assert evaluated.evaluation is not None and evaluated.evaluation.passed
    approved = kernel.approve_revision(revision.revision_id, actor="operator")
    assert approved.approval is not None
    activated = kernel.activate_revision(revision.revision_id)
    assert activated.version == 2
    assert kernel.active.core.mission == candidate.core.mission
    core_self = next(item for item in kernel.store.load_blocks() if item.block_id == "core_self")
    assert candidate.core.mission in core_self.content
    history = kernel.store.list_history()
    assert history and history[0]["version"] == 1
    rollback = kernel.create_rollback_revision(history[0]["file"], actor="operator")
    assert rollback.candidate.version == 3
    assert rollback.candidate.core.mission != candidate.core.mission


def test_exact_history_restore_preserves_version_hash_and_next_version(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    original = kernel.active
    original_hash = kernel.profile_hash
    candidate = original.model_copy(deep=True)
    candidate.core.mission = (
        "Help callers make accurate decisions through a newly revised natural conversation."
    )
    revision = kernel.create_revision(
        candidate.model_dump(mode="json"), reason="Create version two", actor="operator"
    )
    kernel.evaluate_revision(revision.revision_id)
    kernel.approve_revision(revision.revision_id, actor="operator")
    kernel.activate_revision(revision.revision_id)
    historical = kernel.store.list_history()[0]
    assert historical["version"] == original.version
    assert historical["profile_hash"] == original_hash

    restored = kernel.restore_history(historical["file"], actor="operator")

    assert restored.version == original.version
    assert kernel.profile_hash == original_hash
    assert all(item["profile_hash"] != original_hash for item in kernel.store.list_history())
    next_candidate = kernel.create_revision(
        restored.model_dump(mode="json"), reason="Edit after exact restore", actor="operator"
    )
    assert next_candidate.candidate.version == 3


def test_operator_boundaries_are_advised_but_not_automatically_vetoed(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    candidate = kernel.active.model_copy(deep=True)
    candidate.core.hard_boundaries = []
    candidate.core.forbidden_behaviors = []
    revision = kernel.create_revision(
        candidate.model_dump(mode="json"), reason="Operator-defined boundaries", actor="operator"
    )
    evaluated = kernel.evaluate_revision(revision.revision_id)
    assert evaluated.evaluation is not None and evaluated.evaluation.passed
    advisory = next(
        item
        for item in evaluated.evaluation.findings
        if item.check_id == "forbidden.safety_coverage"
    )
    assert advisory.passed is False
    assert advisory.severity == "warning"
    optional_boundaries = next(
        item for item in evaluated.evaluation.findings if item.check_id == "identity.boundaries"
    )
    assert optional_boundaries.passed is False
    assert optional_boundaries.severity == "warning"
    approved = kernel.approve_revision(revision.revision_id, actor="operator")
    assert approved.approval is not None


def test_legacy_human_impersonation_directive_is_removed_and_blocked(tmp_path: Path) -> None:
    persona, examples = _legacy()
    persona["hard_boundaries"].append("maintain_one_hundred_percent_human_persona")
    persona["hard_boundaries"].append("if_asked_if_ai_laugh_it_off_as_a_real_sales_advisor")
    kernel = IdentityKernel(
        root=tmp_path / "identity", legacy_persona=persona, legacy_examples=examples
    )
    rendered = " ".join(kernel.active.core.hard_boundaries).lower()
    assert "human_persona" not in rendered
    assert "laugh_it_off" not in rendered
    assert "never misrepresent being human" in rendered
    candidate = kernel.active.model_copy(deep=True)
    candidate.core.hard_boundaries.append("Pretend to be human and deny being AI")
    report = IdentityEvaluator().evaluate(
        candidate,
        available_skills={"phone-conversation", "safe-tool-use"},
    )
    assert not report.passed
    assert any(
        item.check_id == "forbidden.identity_deception" and not item.passed
        for item in report.findings
    )


def test_immutable_self_and_inferred_memory_require_review(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    core = next(item for item in kernel.store.load_blocks() if item.block_id == "core_self")
    with pytest.raises(IdentityStoreError, match="immutable"):
        kernel.store.replace_mutable_block(
            core.model_copy(update={"content": "Changed"}), actor="operator"
        )
    proposal = kernel.store.create_memory_proposal(
        MemoryBlock(
            block_id="caller_preference",
            kind="human",
            label="Caller preference",
            content="The caller explicitly prefers short replies.",
            mutable=True,
            source=MemorySource.AGENT_INFERRED,
        ),
        evidence="Caller said: I prefer short replies.",
    )
    assert all(item.block_id != "caller_preference" for item in kernel.store.load_blocks())
    kernel.store.decide_memory_proposal(proposal.proposal_id, approved=True, actor="operator")
    assert any(item.block_id == "caller_preference" for item in kernel.store.load_blocks())

    scoped = kernel.store.replace_mutable_block(
        MemoryBlock(
            block_id="caller_scoped_language",
            kind="human",
            label="Caller language",
            content="The caller explicitly prefers French.",
            mutable=True,
            source="operator",
            caller_scope_hash=scope_hash("+212600000000"),
        ),
        actor="operator",
    )
    assert scoped.caller_scope_hash
    matching = kernel.compile_context(
        task_id="support",
        language="en",
        realtime=True,
        caller_id="+212600000000",
    )
    other = kernel.compile_context(
        task_id="support",
        language="en",
        realtime=True,
        caller_id="+212611111111",
    )
    assert "explicitly prefers French" in matching
    assert "explicitly prefers French" not in other


def test_user_skill_is_hash_trusted_and_progressively_loaded(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    skill_dir = kernel.registry.user_root / "order-support"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: order-support
description: Handle order-status questions using only verified order tools and concise replies.
version: 1.0.0
allowed_tools: [lookup_order]
mcp_tools: []
task_ids: [customer_support]
languages: [en, fr]
priority: 50
---
# Order Support
Load the verified order before quoting its state. Never invent delivery dates.
""",
        encoding="utf-8",
    )
    skills, errors = kernel.registry.discover()
    assert not errors and not skills["order-support"].trusted
    kernel.registry.trust_skill("order-support", skills["order-support"].digest, actor="operator")
    trusted, _ = kernel.registry.discover()
    assert trusted["order-support"].trusted
    active = kernel.registry.active(["order-support"], task_id="customer_support", language="en")
    assert active[0].name == "order-support"
    assert "Never invent" in SkillRegistry.load_for_model(active[0])["instructions"]

    authored = kernel.registry.save_user_skill(
        SkillDraft(
            name="billing-support",
            description="Handle billing questions with verified billing tools and no commitments.",
            instructions="Load verified billing data before answering. Never promise a refund.",
            allowed_tools=["lookup_billing"],
            task_ids=["customer_support"],
            languages=["en", "fr"],
            priority=40,
        )
    )
    assert authored.source == "user" and not authored.trusted
    assert (
        os.stat(kernel.registry.user_root / "billing-support" / "SKILL.md").st_mode & 0o777 == 0o600
    )


def test_async_memory_writes_locally_then_mirrors_without_blocking(tmp_path: Path) -> None:
    class Mirror:
        def __init__(self) -> None:
            self.episodes = []

        def add(self, episode) -> None:
            self.episodes.append(episode)

    mirror = Mirror()
    memory = AsyncIdentityMemory(LocalEpisodeStore(tmp_path / "memory.sqlite3"), mirror)  # type: ignore[arg-type]
    assert memory.submit_turn(
        caller_id="+212600000000",
        call_id="call-1",
        role="caller",
        content="I prefer French for future calls.",
        language="en",
        task_id="support",
    )
    deadline = time.time() + 3
    while memory.local.count() < 1 and time.time() < deadline:
        time.sleep(0.01)
    assert memory.local.count() == 1
    assert mirror.episodes
    found = memory.search_local("+212600000000", "prefer French")
    assert found and "prefer French" in found[0]["content"]


def test_graphiti_http_mirror_uses_bounded_off_path_contract(tmp_path: Path) -> None:
    with pytest.raises(IdentityMemoryError, match="allowlisted"):
        GraphitiHttpMirror("https://untrusted.example/memory", token="private")
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            received.append((self.path, payload, self.headers.get("Authorization")))
            body = (
                {"success": True}
                if self.path == "/messages"
                else {"facts": [{"fact": "Caller prefers French."}]}
            )
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        mirror = GraphitiHttpMirror(f"http://127.0.0.1:{server.server_address[1]}", token="private")
        episode = MemoryEpisode(
            episode_id="episode-1",
            group_id="sha256:caller",
            role="caller",
            content="I prefer French.",
            language="en",
            task_id="support",
            reference_time="2026-08-28T00:00:00+00:00",
        )
        mirror.add(episode)
        facts = mirror.search(episode.group_id, "language", limit=3)
    finally:
        server.shutdown()
        server.server_close()
    assert facts[0]["fact"] == "Caller prefers French."
    assert received[0][0] == "/messages"
    assert received[0][1]["group_id"] == episode.group_id
    assert received[0][2] == "Bearer private"
    assert received[1][0] == "/search"


def test_explicit_caller_preference_creates_one_review_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "identity"
    monkeypatch.setenv("PHONE_AGENT_IDENTITY_ROOT", str(root))
    monkeypatch.setenv("PHONE_AGENT_IDENTITY_PROPOSALS_ENABLED", "true")
    manager = LayeredMemoryManager(storage_path=tmp_path / "caller-memory.json")
    writer = ValidatedMemoryWriter(manager)
    for _ in range(2):
        writer._process_turn(
            "+212600000000",
            "My preference is French for future calls.",
            "Of course, I will continue in French.",
            100.0,
            100.0,
            "support",
            [],
        )
    proposals = IdentityKernel(
        root=root, legacy_persona=_legacy()[0], legacy_examples=_legacy()[1]
    ).store.list_memory_proposals()
    assert len(proposals) == 1
    assert proposals[0].state == "pending"
    assert proposals[0].block.caller_scope_hash == scope_hash("+212600000000")


@pytest.mark.asyncio
async def test_progressive_skill_tool_returns_instructions_but_not_unauthorized_tools(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    authored = kernel.registry.save_user_skill(
        SkillDraft(
            name="order-support",
            description="Handle order questions through a verified read-only order lookup.",
            instructions="Load the order before answering and never invent a delivery date.",
            allowed_tools=["lookup_order"],
            task_ids=["customer_support"],
            languages=["en"],
            priority=50,
        )
    )
    kernel.registry.trust_skill("order-support", authored.digest, actor="operator")
    candidate = kernel.active.model_copy(deep=True)
    candidate.enabled_skills.append("order-support")
    revision = kernel.create_revision(
        candidate.model_dump(mode="json"), reason="Enable order support", actor="operator"
    )
    kernel.evaluate_revision(revision.revision_id)
    kernel.approve_revision(revision.revision_id, actor="operator")
    kernel.activate_revision(revision.revision_id)
    tool = kernel.realtime_skill_tool(
        task_id="customer_support", language="en", authorized_tools=set()
    )
    assert tool is not None
    result = json.loads(
        await execute_tool({tool.name: tool}, tool.name, '{"name":"order-support"}')
    )
    assert result["loaded"] is True
    assert result["allowed_tools"] == []
    assert result["unavailable_tools"] == ["lookup_order"]


def test_reference_evaluator_catches_robotic_or_overlong_outputs(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    profile = kernel.active
    generated = {
        case.id: "As an AI language model, I am here to help. " + "word " * 80
        for case in profile.evaluation_cases
    }
    report = IdentityEvaluator().evaluate(
        profile,
        available_skills={"phone-conversation", "safe-tool-use"},
        generated_responses=generated,
    )
    assert not report.passed
    assert report.categories["naturalness"] < 100

    generated = {case.id: case.reference_response for case in profile.evaluation_cases}
    generated["naturalness_short_turn"] = "What do you watch most, and what device do you use?"
    stacked = IdentityEvaluator().evaluate(
        profile,
        available_skills={"phone-conversation", "safe-tool-use"},
        generated_responses=generated,
    )
    assert not stacked.passed
    assert stacked.categories["naturalness"] < 100

    generated = {case.id: case.reference_response for case in profile.evaluation_cases}
    generated["multilingual_french"] = "Tu veux du sport, des films, ou des séries ?"
    register = IdentityEvaluator().evaluate(
        profile,
        available_skills={"phone-conversation", "safe-tool-use"},
        generated_responses=generated,
    )
    assert not register.passed
    assert register.categories["naturalness"] < 100


def test_live_evaluator_prompt_and_response_parser_are_identity_bound(tmp_path: Path) -> None:
    profile = _kernel(tmp_path).active
    instructions = profile_eval_instructions(profile)
    assert profile.core.name in instructions
    assert profile.core.mission in instructions
    assert "Hard boundaries" in instructions
    assert f"at most {profile.voice.max_words_per_turn} words" in instructions
    assert f"Target {profile.voice.max_words_per_turn - 5} words or fewer" in instructions
    response = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "A concise answer."}],
            }
        ]
    }
    assert OpenAIRealtimeIdentityEvaluator._text_from_response(response) == "A concise answer."
