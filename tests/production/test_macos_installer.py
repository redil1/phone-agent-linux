from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_installer_uses_tcc_safe_transactional_runtime() -> None:
    script = (ROOT / "tools" / "install_macos.sh").read_text(encoding="utf-8")

    assert '${HOME}/.local/share/phone-agent/runtime' in script
    assert 'uv venv --python 3.12 --relocatable' in script
    assert 'ProgramArguments": [str(runtime / ".venv/bin/phone-agent-web")]' in script
    assert "grep -Fx '#!/bin/sh'" in script
    assert '"WorkingDirectory": str(runtime)' in script
    assert 'http://127.0.0.1:8090/api/status' in script
    assert "restore_previous_installation" in script
    assert "PHONE_AGENT_IDENTITY_REQUIRE_LIVE_EVAL" not in script


def test_rollback_restores_the_runtime_snapshot() -> None:
    script = (ROOT / "tools" / "rollback_macos.sh").read_text(encoding="utf-8")

    assert '${HOME}/.local/share/phone-agent/runtime' in script
    assert '"${BACKUP_DIR}/runtime"' in script
    assert '"${RUNTIME_TARGET}"' in script
