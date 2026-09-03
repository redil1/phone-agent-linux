from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from phone_agent_gateway.qualification.initial_baseline_rollback import (
    CommandResult,
    RollbackError,
    execute_runtime_rollback,
    load_baseline,
)

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "reports" / "baselines" / "2026-09-02-gsm-cascade-baseline.json"
TARGET = "sha256:082c9bfcfd3b87cfdd02ab53c3dd1ffca61d8a07fe1cb433106c93753e1c26fb"
CANDIDATE = "sha256:" + "c" * 64


def _status(*, call_state: str = "IDLE", llm_model: str | None = None) -> dict[str, Any]:
    return {
        "status": "ok",
        "call_state": call_state,
        "voice_host": {
            "ready": True,
            "configuration_current": True,
            "effective_config": {
                "pipeline_mode": "cascade",
                "stt_provider": "whisper_turbo",
                "stt_model": "large-v3-turbo",
                "llm_provider": "ollama",
                "llm_model": llm_model
                or "hf.co/EryriLabs/phonellm-alpha-1-GGUF:Q4_K_M",
                "tts_provider": "kokoro",
                "tts_model": "hexgrad/Kokoro-82M",
                "tts_voice_id": "af_heart",
            },
        },
    }


class FakeDocker:
    def __init__(self, active: str, *, fail_target: bool = False) -> None:
        self.active = active
        self.fail_target = fail_target
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str], timeout: float) -> CommandResult:
        del timeout
        command = tuple(args)
        self.commands.append(command)
        if command[:3] == ("docker", "image", "inspect"):
            return CommandResult(0, TARGET)
        if command[:2] == ("docker", "inspect"):
            return CommandResult(0, self.active)
        if "config" in command and "--images" in command:
            return CommandResult(0, f"redis:7-alpine\n{TARGET}\n")
        if "up" in command:
            override = Path(command[command.index("-f", 4) + 1]).read_text(encoding="utf-8")
            requested = TARGET if TARGET in override else CANDIDATE
            if requested == TARGET and self.fail_target:
                return CommandResult(1, "", "synthetic target failure")
            self.active = requested
            return CommandResult(0, "ready")
        return CommandResult(1, "", f"unexpected command: {command}")


def test_validation_proves_exact_image_plan_without_mutating_runtime() -> None:
    docker = FakeDocker(TARGET)

    report = execute_runtime_rollback(
        baseline=load_baseline(BASELINE),
        runner=docker,
        status_loader=_status,
    )

    assert report["qualified"] is True
    assert report["action"] == "validation_only"
    assert report["runtime"]["active_image_after"] == TARGET
    assert not any("up" in command for command in docker.commands)


def test_apply_replaces_only_core_with_exact_baseline_and_checks_readback() -> None:
    docker = FakeDocker(CANDIDATE)

    report = execute_runtime_rollback(
        baseline=load_baseline(BASELINE),
        runner=docker,
        status_loader=_status,
        apply=True,
        timeout=3,
    )

    assert report["qualified"] is True
    assert report["action"] == "restored_baseline_image"
    apply_command = next(command for command in docker.commands if "up" in command)
    assert "--no-deps" in apply_command
    assert "--no-build" in apply_command
    assert apply_command[-1] == "phoneagent"


def test_apply_refuses_an_active_call_before_any_runtime_mutation() -> None:
    docker = FakeDocker(CANDIDATE)

    with pytest.raises(RollbackError, match="while a call is active"):
        execute_runtime_rollback(
            baseline=load_baseline(BASELINE),
            runner=docker,
            status_loader=lambda: _status(call_state="ACTIVE"),
            apply=True,
        )

    assert not any("up" in command for command in docker.commands)


def test_failed_target_restores_the_previous_runtime_image() -> None:
    docker = FakeDocker(CANDIDATE, fail_target=True)

    with pytest.raises(RollbackError, match="previous runtime image was restored"):
        execute_runtime_rollback(
            baseline=load_baseline(BASELINE),
            runner=docker,
            status_loader=_status,
            apply=True,
            timeout=3,
        )

    assert docker.active == CANDIDATE
    assert sum("up" in command for command in docker.commands) == 2


def test_configuration_drift_fails_qualification() -> None:
    docker = FakeDocker(TARGET)

    report = execute_runtime_rollback(
        baseline=load_baseline(BASELINE),
        runner=docker,
        status_loader=lambda: _status(llm_model="wrong/model"),
    )

    assert report["qualified"] is False
    assert any(
        check["name"] == "effective_llm_model" and check["status"] == "fail"
        for check in report["checks"]
    )
