"""Fail-closed runtime rollback to the frozen Milestone 0 GSM baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "reports" / "baselines" / "2026-09-02-gsm-cascade-baseline.json"
DEFAULT_COMPOSE = ROOT / "compose.production.yaml"
DEFAULT_STATUS_URL = "http://127.0.0.1:8090/api/status"


class RollbackError(RuntimeError):
    """Raised when rollback cannot be proved or completed safely."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str
    evidence: str


CommandRunner = Callable[[Sequence[str], float], CommandResult]
StatusLoader = Callable[[], dict[str, Any]]


def _local_command(args: Sequence[str], timeout: float) -> CommandResult:
    completed = subprocess.run(
        list(args), capture_output=True, text=True, timeout=timeout, check=False
    )
    return CommandResult(completed.returncode, completed.stdout.strip(), completed.stderr.strip())


def _status_loader(url: str = DEFAULT_STATUS_URL) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = cast(object, json.loads(response.read().decode("utf-8")))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RollbackError("the authoritative runtime status endpoint is unavailable") from exc
    if not isinstance(payload, dict):
        raise RollbackError("the authoritative runtime status response is invalid")
    return cast(dict[str, Any], payload)


def load_baseline(path: Path = DEFAULT_BASELINE) -> dict[str, Any]:
    try:
        payload = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise RollbackError("the frozen baseline report is unavailable or invalid") from exc
    if not isinstance(payload, dict):
        raise RollbackError("the frozen baseline schema is unsupported")
    baseline = cast(dict[str, Any], payload)
    if baseline.get("schema_version") != 1:
        raise RollbackError("the frozen baseline schema is unsupported")
    required = {"baseline_id", "production_container", "pipeline", "android", "phone_link"}
    if not required <= set(baseline):
        raise RollbackError("the frozen baseline report is incomplete")
    return baseline


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RollbackError(f"{label} is missing from the frozen baseline")
    return cast(dict[str, Any], value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RollbackError(f"{label} is missing from the frozen baseline")
    return value.strip()


def _run(
    runner: CommandRunner, args: Sequence[str], *, timeout: float = 30, label: str
) -> str:
    result = runner(args, timeout)
    if result.returncode != 0:
        detail = result.stderr or result.stdout or "no diagnostic"
        raise RollbackError(f"{label} failed: {detail[:300]}")
    return result.stdout.strip()


def _target_details(baseline: dict[str, Any]) -> tuple[str, str, dict[str, str]]:
    container = _mapping(baseline["production_container"], "production_container")
    pipeline = _mapping(baseline["pipeline"], "pipeline")
    stt = _mapping(pipeline.get("stt"), "pipeline.stt")
    llm = _mapping(pipeline.get("llm"), "pipeline.llm")
    tts = _mapping(pipeline.get("tts"), "pipeline.tts")
    expected = {
        "pipeline_mode": "cascade",
        "stt_provider": _text(stt.get("provider"), "pipeline.stt.provider"),
        "stt_model": _text(stt.get("configured_model"), "pipeline.stt.configured_model"),
        "llm_provider": _text(llm.get("provider"), "pipeline.llm.provider"),
        "llm_model": _text(llm.get("model"), "pipeline.llm.model"),
        "tts_provider": _text(tts.get("provider"), "pipeline.tts.provider"),
        "tts_model": _text(tts.get("model"), "pipeline.tts.model"),
        "tts_voice_id": _text(tts.get("voice"), "pipeline.tts.voice"),
    }
    return (
        _text(container.get("name"), "production_container.name"),
        _text(container.get("image_id"), "production_container.image_id"),
        expected,
    )


def _compose_override(image_id: str) -> str:
    if not image_id.startswith("sha256:") or len(image_id) != 71:
        raise RollbackError("the baseline container image ID is not a full SHA-256")
    return (
        "services:\n"
        "  phoneagent:\n"
        f'    image: "{image_id}"\n'
        "    pull_policy: never\n"
    )


def _active_image(runner: CommandRunner, container_name: str) -> str:
    return _run(
        runner,
        ("docker", "inspect", "--format", "{{.Image}}", container_name),
        label="active container inspection",
    )


def _check_status(status: dict[str, Any], expected: dict[str, str]) -> list[Check]:
    voice = _mapping(status.get("voice_host"), "status.voice_host")
    effective = _mapping(voice.get("effective_config"), "status.voice_host.effective_config")
    checks = [
        Check("api_health", "pass" if status.get("status") == "ok" else "fail", str(status.get("status"))),
        Check("call_idle", "pass" if status.get("call_state") == "IDLE" else "fail", str(status.get("call_state"))),
        Check("voice_host_ready", "pass" if voice.get("ready") is True else "fail", str(voice.get("ready"))),
        Check(
            "worker_configuration_current",
            "pass" if voice.get("configuration_current") is True else "fail",
            str(voice.get("configuration_current")),
        ),
    ]
    for field, wanted in expected.items():
        actual = str(effective.get(field, ""))
        checks.append(Check(f"effective_{field}", "pass" if actual == wanted else "fail", actual))
    return checks


def _wait_for_status(loader: StatusLoader, expected: dict[str, str], timeout: float) -> tuple[dict[str, Any], list[Check]]:
    deadline = time.monotonic() + timeout
    last_error = "status did not become ready"
    while time.monotonic() < deadline:
        try:
            status = loader()
            checks = _check_status(status, expected)
            if all(check.status == "pass" for check in checks):
                return status, checks
            last_error = ", ".join(check.name for check in checks if check.status == "fail")
        except RollbackError as exc:
            last_error = str(exc)
        time.sleep(1)
    raise RollbackError(f"baseline worker verification timed out: {last_error}")


def _compose_plan(
    runner: CommandRunner, compose_file: Path, override_file: Path, image_id: str
) -> Check:
    images = _run(
        runner,
        (
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "-f",
            str(override_file),
            "config",
            "--images",
        ),
        label="rollback Compose plan rendering",
    ).splitlines()
    occurrences = sum(value.strip() == image_id for value in images)
    return Check("compose_targets_exact_image", "pass" if occurrences == 1 else "fail", str(occurrences))


def _apply_image(
    runner: CommandRunner, compose_file: Path, override_file: Path, *, timeout: float
) -> None:
    _run(
        runner,
        (
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "-f",
            str(override_file),
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            str(max(1, int(timeout))),
            "phoneagent",
        ),
        timeout=timeout + 30,
        label="runtime rollback",
    )


def execute_runtime_rollback(
    *,
    baseline: dict[str, Any],
    compose_file: Path = DEFAULT_COMPOSE,
    runner: CommandRunner = _local_command,
    status_loader: StatusLoader = _status_loader,
    apply: bool = False,
    timeout: float = 180,
) -> dict[str, Any]:
    """Validate or apply exact-image rollback and return sanitized evidence."""

    if not compose_file.is_file():
        raise RollbackError("the production Compose file is unavailable")
    container_name, target_image, expected = _target_details(baseline)
    resolved_target = _run(
        runner,
        ("docker", "image", "inspect", "--format", "{{.Id}}", target_image),
        label="baseline image inspection",
    )
    if resolved_target != target_image:
        raise RollbackError("the local baseline image does not match its frozen image ID")

    active_before = _active_image(runner, container_name)
    initial_status = status_loader()
    initial_checks = _check_status(initial_status, expected)
    call_check = next(check for check in initial_checks if check.name == "call_idle")
    if apply and call_check.status != "pass":
        raise RollbackError("refusing runtime rollback while a call is active")

    with tempfile.TemporaryDirectory(prefix="phoneagent-baseline-rollback-") as directory:
        override = Path(directory) / "compose.rollback.yaml"
        override.write_text(_compose_override(target_image), encoding="utf-8")
        plan_check = _compose_plan(runner, compose_file, override, target_image)
        if plan_check.status != "pass":
            raise RollbackError("the rendered Compose plan does not select the exact baseline image")

        action = "validation_only"
        fallback_exercised = False
        if apply and active_before != target_image:
            action = "restored_baseline_image"
            try:
                _apply_image(runner, compose_file, override, timeout=timeout)
                active_after = _active_image(runner, container_name)
                if active_after != target_image:
                    raise RollbackError("runtime restarted on an unexpected image")
                final_status, final_checks = _wait_for_status(status_loader, expected, timeout)
            except Exception as rollback_error:
                fallback_exercised = True
                fallback = Path(directory) / "compose.restore-previous.yaml"
                fallback.write_text(_compose_override(active_before), encoding="utf-8")
                _apply_image(runner, compose_file, fallback, timeout=timeout)
                if _active_image(runner, container_name) != active_before:
                    raise RollbackError(
                        "baseline rollback failed and the previous runtime could not be restored"
                    ) from rollback_error
                raise RollbackError(
                    "baseline rollback failed; the previous runtime image was restored"
                ) from rollback_error
        else:
            active_after = active_before
            final_status = initial_status
            final_checks = initial_checks
            if apply:
                action = "verified_already_at_baseline"

    checks = [
        Check("baseline_image_present", "pass", resolved_target),
        plan_check,
        Check(
            "active_image_exact",
            "pass" if active_after == target_image else "fail",
            active_after,
        ),
        *final_checks,
    ]
    qualified = all(check.status == "pass" for check in checks)
    android = _mapping(baseline["android"], "android")
    phone_link = _mapping(baseline["phone_link"], "phone_link")
    report: dict[str, Any] = {
        "schema_version": 1,
        "baseline_id": _text(baseline["baseline_id"], "baseline_id"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": "apply" if apply else "validate",
        "action": action,
        "qualified": qualified,
        "contains_customer_data": False,
        "runtime": {
            "container": container_name,
            "target_image_id": target_image,
            "active_image_before": active_before,
            "active_image_after": active_after,
            "fallback_exercised": fallback_exercised,
            "call_state_after": final_status.get("call_state"),
        },
        "android_recovery_anchors": {
            "installed_update_apk_sha256": android.get("installed_update_apk_sha256"),
            "baked_system_apk_sha256": android.get("baked_system_apk_sha256"),
            "package": android.get("package"),
            "protocol": phone_link.get("protocol"),
        },
        "checks": [asdict(check) for check in checks],
    }
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    report["report_sha256"] = hashlib.sha256(canonical).hexdigest()
    return report


def _write_report(report: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--compose", type=Path, default=DEFAULT_COMPOSE)
    parser.add_argument("--status-url", default=DEFAULT_STATUS_URL)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply-runtime", action="store_true")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--require-qualified", action="store_true")
    args = parser.parse_args()
    report = execute_runtime_rollback(
        baseline=load_baseline(args.baseline),
        compose_file=args.compose,
        status_loader=lambda: _status_loader(args.status_url),
        apply=args.apply_runtime,
        timeout=args.timeout,
    )
    if args.output:
        _write_report(report, args.output)
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.require_qualified and not report["qualified"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
