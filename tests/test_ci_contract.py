"""CI must remain executable locally and fail closed around real devices."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RUNNER = ROOT / "ci" / "run-stage.sh"

REQUIRED_JOBS = {
    "quality",
    "fast-unit",
    "integration",
    "package",
    "android-protocol",
    "security",
    "licence-sbom",
    "container",
    "eval",
    "device-qualification",
}


def _workflow() -> dict[str, object]:
    payload = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(payload, dict)
    return payload


def test_ci_exposes_every_required_stage_with_immutable_actions() -> None:
    payload = _workflow()
    assert payload["permissions"] == {"contents": "read"}
    jobs = payload["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == REQUIRED_JOBS

    source = WORKFLOW.read_text(encoding="utf-8")
    action_refs = re.findall(r"uses:\s*([^\s#]+)", source)
    assert action_refs
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref) for ref in action_refs)


def test_ci_jobs_share_the_local_stage_contract() -> None:
    payload = _workflow()
    jobs = payload["jobs"]
    assert isinstance(jobs, dict)
    for stage in REQUIRED_JOBS - {"device-qualification"}:
        assert f"./ci/run-stage.sh {stage}" in str(jobs[stage])

    runner = RUNNER.read_text(encoding="utf-8")
    for stage in REQUIRED_JOBS:
        assert f'"{stage}")' in runner

    source = WORKFLOW.read_text(encoding="utf-8")
    assert "--extra dev" not in source
    assert source.count("uv sync --locked --dev") == 5
    assert "uv sync --locked --all-extras --no-dev" in source
    assert "PHONE_AGENT_FULL_CONTAINER_BUILD" in source

    runner = RUNNER.read_text(encoding="utf-8")
    assert "uv tool run --from pip-audit==2.10.1" in runner
    assert "--no-emit-package en-core-web-sm" in runner
    assert "--all-extras --no-dev" in runner
    assert "ci/prepare_audit_requirements.py" in runner
    assert "ci/validate_dependency_policy.py" in runner
    assert "ci/validate_feature_flags.py" in runner
    assert "ci/validate_s2s_inventory.py" in runner
    assert "s2s-inventory-validation.json" in runner
    assert "ci/validate_cascade_characterization.py" in runner
    assert "cascade-characterization-validation.json" in runner
    assert "feature-flag-validation.json" in runner
    assert "release/evidence.py" in runner
    assert "release-evidence-validation.json" in runner
    assert "reports/releases/m0-clean-baseline" in runner
    assert "m0-clean-baseline-validation.json" in runner
    assert "m0-exit-signature-validation.json" in runner
    assert "m0-local-qualification-ed25519.pem" in runner
    assert "qualification.performance_harness" in runner
    assert "linux-x86_64-contract-ci" in runner
    assert "cascade-performance.json" in runner
    assert "--require-qualified" in runner
    assert "SOURCE_DATE_EPOCH" in runner
    assert "release.normalize_sdist" in runner


def test_device_qualification_is_manual_self_hosted_and_fail_closed() -> None:
    payload = _workflow()
    dispatch = payload["on"]["workflow_dispatch"]
    assert dispatch["inputs"]["run_device"]["default"] == "false"

    device = payload["jobs"]["device-qualification"]
    assert "workflow_dispatch" in device["if"]
    assert "inputs.run_device" in device["if"]
    assert "self-hosted" in device["runs-on"]
    assert device["environment"] == "device-qualification"
    assert device["env"]["PHONE_AGENT_DEVICE_CI"] == "1"

    runner = RUNNER.read_text(encoding="utf-8")
    assert 'PHONE_AGENT_DEVICE_CI:-0' in runner
    assert "phone-agent-qualify" in runner
    assert "dial" not in runner.lower()


def test_android_builds_target_the_ci_java_contract() -> None:
    scripts = (
        ROOT / "android_service_apk" / "build_and_install.sh",
        ROOT / "android_service_apk" / "test_protocol_codec.sh",
        ROOT / "android_service_apk" / "test_remote_link.sh",
    )
    assert all("javac --release 17" in path.read_text(encoding="utf-8") for path in scripts)
