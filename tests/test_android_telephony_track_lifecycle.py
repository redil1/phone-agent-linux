"""Source-level guards for the privileged Android Telephony-TX lifecycle."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "android_service_apk/src/com/phoneagent/gateway/DigitalAudioBridge.java"
CALL_MANAGER = ROOT / "android_service_apk/src/com/phoneagent/gateway/CallManager.java"
PHH_SU_PROVISIONER = ROOT / "android_service_apk/provision_phh_su_audio_recovery.sh"


def test_android_never_retries_physical_telephony_track_creation() -> None:
    source = BRIDGE.read_text(encoding="utf-8")

    assert "private static final int TRACK_START_ATTEMPTS = 1;" in source
    assert "telephonyTrackStartAttempts.incrementAndGet();" in source


def test_call_end_schedules_native_telephony_track_cleanup() -> None:
    bridge = BRIDGE.read_text(encoding="utf-8")
    calls = CALL_MANAGER.read_text(encoding="utf-8")

    assert "public static void onCellularCallEnded()" in bridge
    assert '"su", "-c", "killall audioserver", "0"' in bridge
    assert 'result.put("audioserver_recoveries", audioServerRecoveries.get());' in bridge
    assert "DigitalAudioBridge.onCellularCallEnded();" in calls


def test_phh_su_policy_is_command_scoped_and_installer_managed() -> None:
    provisioner = PHH_SU_PROVISIONER.read_text(encoding="utf-8")
    installer = (ROOT / "android_service_apk/build_and_install.sh").read_text(
        encoding="utf-8"
    )

    assert 'RECOVERY_COMMAND="killall audioserver"' in provisioner
    assert "INSERT INTO uid_policy (uid, policy, until, command)" in provisioner
    assert "command='$RECOVERY_COMMAND'" in provisioner
    assert 'provision_phh_su_audio_recovery.sh" "$DEV_ID"' in installer
