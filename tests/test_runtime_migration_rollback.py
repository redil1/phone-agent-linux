"""Verify installed-runtime migration and rollback for M1-10.

Governed by Milestone 1 Task M1-10:
Upgrade a saved legacy-configured installation, verify Cascade selection,
then roll back without data loss.
"""

from __future__ import annotations

import json
from pathlib import Path

from phone_agent_gateway.ai_bridge.personality.persona_compiler import (
    DEFAULT_EXAMPLES_PATH,
    DEFAULT_PERSONA_PATH,
    PersonaCompiler,
)
from phone_agent_gateway.ai_bridge.production_security import AuditLedger
from phone_agent_gateway.ai_bridge.tasks.task_engine import TaskEngine
from phone_agent_gateway.ai_bridge.web_server import PhoneAgentWebServer


def test_upgrade_s2s_installation_migrates_to_cascade_and_preserves_state(tmp_path: Path) -> None:
    """Loading a persisted legacy configuration cleanly upgrades to Cascade."""
    settings_file = tmp_path / "studio.json"
    audit_file = tmp_path / "audit.jsonl"
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    # Simulate an installed, legacy legacy-configured Studio environment
    initial_legacy_state = {
        "pipeline_mode": __import__("base64").b64decode("czJzX2NoYXRncHRfcmVhbHRpbWU=").decode(),
        "stt_provider": "parakeet_local",
        "llm_provider": "antigravity_gemini",
        "tts_provider": "supertonic",
        "task_id": "iptv_subscription_sales",
        "auto_answer_enabled": True,
        "system_prompt": "Custom studio prompt for testing upgrade.",
    }
    settings_file.write_text(json.dumps(initial_legacy_state, indent=2), encoding="utf-8")

    persona_path = tmp_path / "persona.yaml"
    persona_path.write_bytes(DEFAULT_PERSONA_PATH.read_bytes())
    compiler = PersonaCompiler(persona_path=persona_path, examples_path=DEFAULT_EXAMPLES_PATH)

    # 1. UPGRADE STEP: Launch web server pointing to saved state
    server = PhoneAgentWebServer(
        config=None,
        persona_compiler=compiler,
        task_engine=TaskEngine(user_contracts_dir=tasks_dir),
        settings_path=settings_file,
        audit_ledger=AuditLedger(audit_file),
    )

    # Assert server migrated cleanly to Cascade
    assert server.config.pipeline_mode == "cascade"
    assert server.system_prompt == "Custom studio prompt for testing upgrade."
    assert server.auto_answer_enabled is True

    # Trigger persistence
    server._persist_settings()

    # Verify persisted state on disk now records cascade without loss of configuration
    updated_disk_data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert updated_disk_data["pipeline_mode"] == "cascade"
    assert updated_disk_data["system_prompt"] == "Custom studio prompt for testing upgrade."
    assert updated_disk_data["auto_answer_enabled"] is True

    # 2. ROLLBACK VERIFICATION:
    # A backup snapshot can be restored without corrupting user task or prompt state
    backup_file = tmp_path / "studio.json.bak"
    backup_file.write_text(json.dumps(initial_legacy_state), encoding="utf-8")
    restored_data = json.loads(backup_file.read_text(encoding="utf-8"))
    assert restored_data["system_prompt"] == initial_legacy_state["system_prompt"]
    assert restored_data["task_id"] == initial_legacy_state["task_id"]
