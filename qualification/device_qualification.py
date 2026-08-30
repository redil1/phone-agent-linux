"""Produce a machine-readable, evidence-backed Android qualification report."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

DEFAULT_PROFILE = Path(__file__).resolve().parent / "devices" / "redmi-12c-earth-gsi.json"
DEFAULT_REPORT_DIR = Path.home() / ".local" / "share" / "phone-agent" / "qualification"


class QualificationError(RuntimeError):
    pass


class CommandRunner(Protocol):
    def run(self, *args: str, timeout: float = 10.0) -> str: ...


class AdbRunner:
    def __init__(self, device_id: str | None = None) -> None:
        executable = shutil.which("adb")
        if executable is None:
            raise QualificationError("adb was not found")
        self.executable = executable
        self.device_id = device_id or self._select_device()

    def _select_device(self) -> str:
        result = subprocess.run(
            [self.executable, "devices", "-l"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        devices = [
            fields[0]
            for line in result.stdout.splitlines()[1:]
            if len(fields := line.split()) >= 2 and fields[1] == "device"
        ]
        if len(devices) != 1:
            raise QualificationError("exactly one authorized Android device is required")
        return devices[0]

    def run(self, *args: str, timeout: float = 10.0) -> str:
        result = subprocess.run(
            [self.executable, "-s", self.device_id, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            raise QualificationError(f"adb command failed: {' '.join(args[:3])}")
        return result.stdout.strip()


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str
    evidence: str
    required: bool = True


def _check(
    name: str, passed: bool, evidence: Any, *, required: bool = True, warning: bool = False
) -> Check:
    status = "pass" if passed else ("warn" if warning or not required else "fail")
    return Check(name, status, str(evidence)[:500], required)


def _http_json(path: str, *, port: int = 8765) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise QualificationError(f"gateway endpoint {path} is unavailable") from exc
    if not isinstance(value, dict):
        raise QualificationError(f"gateway endpoint {path} returned invalid JSON")
    return value


def load_profile(path: Path = DEFAULT_PROFILE) -> dict[str, Any]:
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualificationError("device profile is unavailable or invalid") from exc
    required = {
        "version",
        "profile_id",
        "allowed_devices",
        "allowed_models",
        "allowed_sdk_levels",
        "allowed_abis",
        "allowed_fingerprints",
        "required_package",
        "required_audio_format",
        "required_protocol",
    }
    if not isinstance(profile, dict) or set(profile) != required or profile["version"] != 1:
        raise QualificationError("device profile fields or version are invalid")
    return profile


def qualify_device(
    runner: CommandRunner,
    *,
    profile: dict[str, Any],
    health: dict[str, Any],
    audio: dict[str, Any],
) -> dict[str, Any]:
    def getprop(name: str) -> str:
        return runner.run("shell", "getprop", name)
    model = getprop("ro.product.model")
    device = getprop("ro.product.device")
    sdk_text = getprop("ro.build.version.sdk")
    fingerprint = getprop("ro.build.fingerprint")
    abi = getprop("ro.product.cpu.abi")
    root_id = runner.run("shell", "su", "-c", "id")
    package = runner.run("shell", "pm", "path", profile["required_package"])
    package_dump = runner.run("shell", "dumpsys", "package", profile["required_package"])
    dialers = runner.run("shell", "cmd", "role", "get-role-holders", "android.app.role.DIALER")
    version_match = re.search(r"versionName=([^\s]+)", package_dump)
    sdk = int(sdk_text) if sdk_text.isdigit() else -1
    audio_from_health = health.get("audio") if isinstance(health.get("audio"), dict) else {}
    checks = _build_checks(
        profile=profile,
        model=model,
        device=device,
        sdk=sdk,
        sdk_text=sdk_text,
        fingerprint=fingerprint,
        abi=abi,
        root_id=root_id,
        package=package,
        package_dump=package_dump,
        dialers=dialers,
        health=health,
        audio=audio,
        audio_from_health=audio_from_health,
    )
    failed = [item.name for item in checks if item.required and item.status == "fail"]
    warnings = [item.name for item in checks if item.status == "warn"]
    report: dict[str, Any] = {
        "version": 1,
        "profile_id": profile["profile_id"],
        "qualified": not failed,
        "generated_unix": int(time.time()),
        "device": {
            "serial_hash": "sha256:"
            + hashlib.sha256(str(getattr(runner, "device_id", "unknown")).encode()).hexdigest()[
                :16
            ],
            "model": model,
            "device": device,
            "sdk": sdk,
            "fingerprint": fingerprint,
            "abi": abi,
            "gateway_version": version_match.group(1) if version_match else "unknown",
        },
        "gateway": {
            "state": health.get("state"),
            "audio_format": audio.get("network_format"),
            "protocol": audio.get("protocol"),
        },
        "checks": [asdict(item) for item in checks],
        "failed_checks": failed,
        "warnings": warnings,
    }
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"))
    report["report_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    return report


def _build_checks(**values: Any) -> list[Check]:
    p, h, a = values["profile"], values["health"], values["audio"]
    return [
        _check("device_profile", values["device"] in p["allowed_devices"], values["device"]),
        _check("model_profile", values["model"] in p["allowed_models"], values["model"]),
        _check("android_sdk", values["sdk"] in p["allowed_sdk_levels"], values["sdk_text"]),
        _check(
            "build_fingerprint",
            values["fingerprint"] in p["allowed_fingerprints"],
            values["fingerprint"],
        ),
        _check("cpu_abi", values["abi"] in p["allowed_abis"], values["abi"]),
        _check("root_available", "uid=0(root)" in values["root_id"], values["root_id"]),
        _check("gateway_package", values["package"].startswith("package:"), values["package"]),
        _check(
            "gateway_privileged",
            "PRIVILEGED" in values["package_dump"] and "SYSTEM" in values["package_dump"],
            "SYSTEM/PRIVILEGED flags",
        ),
        _check(
            "dialer_role",
            p["required_package"] in values["dialers"].splitlines(),
            values["dialers"],
        ),
        _check("gateway_health", h.get("status") == "ok", h.get("status")),
        _check("gateway_ready", h.get("gateway") == "ready", h.get("gateway")),
        _check(
            "link_key_provisioned",
            h.get("link_key_provisioned") is True,
            h.get("link_key_provisioned"),
        ),
        _check(
            "capture_permission",
            a.get("capture_audio_output_granted") is True,
            a.get("capture_audio_output_granted"),
        ),
        _check(
            "telephony_output",
            a.get("telephony_output_present") is True,
            a.get("telephony_output_present"),
        ),
        _check(
            "network_audio_format",
            a.get("network_format") == p["required_audio_format"],
            a.get("network_format"),
        ),
        _check(
            "authenticated_protocol", a.get("protocol") == p["required_protocol"], a.get("protocol")
        ),
        _check(
            "audio_service", a.get("status") == "ok" and a.get("running") is True, a.get("status")
        ),
        _check("audio_last_error", not a.get("last_error"), a.get("last_error") or "none"),
        _check(
            "historical_playout_underruns",
            int(a.get("audio_track_underruns") or 0) == 0,
            a.get("audio_track_underruns", 0),
            required=False,
            warning=True,
        ),
        _check(
            "historical_mid_speech_starvation",
            int(a.get("mid_speech_starvation_events") or 0) == 0,
            a.get("mid_speech_starvation_events", 0),
            required=False,
            warning=True,
        ),
        _check(
            "health_audio_consistency",
            not values["audio_from_health"]
            or values["audio_from_health"].get("link_epoch") == a.get("link_epoch"),
            a.get("link_epoch", "missing"),
        ),
    ]


def _write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Formally qualify a PhoneAgent Android device")
    parser.add_argument("--device-id", default=os.getenv("PHONE_AGENT_DEVICE_ID"))
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ensure-forwards", action="store_true")
    args = parser.parse_args()
    runner = AdbRunner(args.device_id)
    if args.ensure_forwards:
        for port in (8765, 8766, 8767, 8768):
            runner.run("forward", f"tcp:{port}", f"tcp:{port}")
    report = qualify_device(
        runner,
        profile=load_profile(args.profile),
        health=_http_json("/health"),
        audio=_http_json("/audio/status"),
    )
    output = args.output or (
        DEFAULT_REPORT_DIR / f"qualification-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    )
    _write_report(report, output)
    print(json.dumps({"qualified": report["qualified"], "report": str(output)}))
    if not report["qualified"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
