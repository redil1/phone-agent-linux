from __future__ import annotations

from typing import Any

from phone_agent_gateway.qualification.device_qualification import qualify_device


class FakeRunner:
    device_id = "serial-secret"

    def __init__(self, values: dict[tuple[str, ...], str]) -> None:
        self.values = values

    def run(self, *args: str, timeout: float = 10.0) -> str:
        return self.values[args]


def _profile() -> dict[str, Any]:
    return {
        "version": 1,
        "profile_id": "test-phone",
        "allowed_devices": ["device-a"],
        "allowed_models": ["Model A"],
        "allowed_sdk_levels": [34],
        "allowed_abis": ["arm64-v8a"],
        "allowed_fingerprints": ["vendor/device/build"],
        "required_package": "com.phoneagent.gateway",
        "required_audio_format": "pcm_s16le_16000_mono",
        "required_protocol": "phag_v1_hmac_sha256",
    }


def _runner() -> FakeRunner:
    return FakeRunner(
        {
            ("shell", "getprop", "ro.product.model"): "Model A",
            ("shell", "getprop", "ro.product.device"): "device-a",
            ("shell", "getprop", "ro.build.version.sdk"): "34",
            ("shell", "getprop", "ro.build.fingerprint"): "vendor/device/build",
            ("shell", "getprop", "ro.product.cpu.abi"): "arm64-v8a",
            ("shell", "su", "-c", "id"): "uid=0(root)",
            (
                "shell",
                "pm",
                "path",
                "com.phoneagent.gateway",
            ): "package:/system/priv-app/PhoneAgentGateway.apk",
            (
                "shell",
                "dumpsys",
                "package",
                "com.phoneagent.gateway",
            ): "versionName=1.0.0 flags=[ SYSTEM ] privateFlags=[ PRIVILEGED ]",
            (
                "shell",
                "cmd",
                "role",
                "get-role-holders",
                "android.app.role.DIALER",
            ): "com.phoneagent.gateway",
        }
    )


def _health() -> dict[str, Any]:
    return {
        "status": "ok",
        "gateway": "ready",
        "state": "IDLE",
        "link_key_provisioned": True,
        "audio": {"link_epoch": "epoch-1"},
    }


def _audio() -> dict[str, Any]:
    return {
        "status": "ok",
        "running": True,
        "capture_audio_output_granted": True,
        "telephony_output_present": True,
        "network_format": "pcm_s16le_16000_mono",
        "protocol": "phag_v1_hmac_sha256",
        "last_error": "",
        "audio_track_underruns": 0,
        "mid_speech_starvation_events": 0,
        "link_epoch": "epoch-1",
    }


def test_matching_device_is_qualified_and_serial_is_redacted() -> None:
    report = qualify_device(_runner(), profile=_profile(), health=_health(), audio=_audio())
    assert report["qualified"] is True
    assert report["failed_checks"] == []
    assert report["device"]["serial_hash"].startswith("sha256:")
    assert "serial-secret" not in str(report)
    assert len(report["report_sha256"]) == 64


def test_audio_protocol_mismatch_fails_qualification() -> None:
    audio = _audio()
    audio["protocol"] = "unauthenticated"
    report = qualify_device(_runner(), profile=_profile(), health=_health(), audio=audio)
    assert report["qualified"] is False
    assert "authenticated_protocol" in report["failed_checks"]
