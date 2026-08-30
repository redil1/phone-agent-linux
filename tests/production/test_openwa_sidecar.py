from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_openwa_sidecar_is_pinned_local_and_hardened() -> None:
    compose_path = ROOT / "integrations" / "openwa" / "compose.yaml"
    raw = compose_path.read_text(encoding="utf-8")
    compose = yaml.safe_load(raw)
    service = compose["services"]["openwa"]

    assert service["image"].startswith("ghcr.io/rmyndharis/openwa:0.23.3@sha256:")
    assert ":latest" not in service["image"]
    assert service["ports"] == ["127.0.0.1:${OPENWA_PORT:-2785}:2785"]
    assert service["read_only"] is True
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["cap_drop"] == ["ALL"]
    assert service["restart"] == "unless-stopped"
    assert service["environment"]["MCP_ENABLED"] == "true"
    assert service["environment"]["SEND_PACING_ENABLED"] == "true"
    assert service["environment"]["NODE_ID"] == "phoneagent-openwa"
    assert service["environment"]["ENGINE_TYPE"] == "baileys"
    assert service["environment"]["AUTO_START_SESSIONS"] == "true"
    assert service["healthcheck"]["retries"] >= 3
    assert "phoneagent-openwa-data:/app/data" in service["volumes"]


def test_openwa_sidecar_scripts_preserve_data_and_private_secrets() -> None:
    install = (ROOT / "tools" / "install_openwa_sidecar.sh").read_text(encoding="utf-8")
    stop = (ROOT / "tools" / "stop_openwa_sidecar.sh").read_text(encoding="utf-8")

    assert 'openssl rand -base64 48' in install
    assert 'os.O_EXCL, 0o600' in install
    assert 'chmod 600 "${ENV_FILE}"' in install
    assert 'docker compose --env-file "${ENV_FILE}"' in install
    assert 'http://127.0.0.1:2785/api/health/ready' in install
    assert " down" not in install
    assert " -v" not in stop
    assert 'docker compose --env-file "${ENV_FILE}"' in stop
