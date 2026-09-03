"""Source-level guards for the privileged Android Telephony-TX lifecycle."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "android_service_apk/src/com/phoneagent/gateway/DigitalAudioBridge.java"
CALL_MANAGER = ROOT / "android_service_apk/src/com/phoneagent/gateway/CallManager.java"
PHH_SU_PROVISIONER = ROOT / "android_service_apk/provision_phh_su_audio_recovery.sh"
REMOTE_LINK = ROOT / "android_service_apk/src/com/phoneagent/gateway/RemoteLinkService.java"
PROTOCOL_CODEC = ROOT / "android_service_apk/src/com/phoneagent/gateway/ProtocolCodec.java"
PYTHON_REMOTE_LINK = ROOT / "ai_bridge/remote_link.py"
FRAMED_LINK = ROOT / "mac_client/framed_link.py"


def test_android_never_retries_physical_telephony_track_creation() -> None:
    source = BRIDGE.read_text(encoding="utf-8")

    assert "private static final int TRACK_START_ATTEMPTS = 1;" in source
    assert "telephonyTrackStartAttempts.incrementAndGet();" in source


def test_android_requires_the_active_track_to_be_routed_to_telephony() -> None:
    source = BRIDGE.read_text(encoding="utf-8")

    assert "track.getRoutedDevice()" in source
    assert "routedDevice.getType() != AudioDeviceInfo.TYPE_TELEPHONY" in source
    assert 'injectionProof = "android_audio_policy_routed_to_telephony";' in source


def test_call_end_schedules_native_telephony_track_cleanup() -> None:
    bridge = BRIDGE.read_text(encoding="utf-8")
    calls = CALL_MANAGER.read_text(encoding="utf-8")

    assert "public static void onCellularCallEnded()" in bridge
    assert '"su", "-c", AUDIO_SERVER_RECOVERY_COMMAND, "0"' in bridge
    assert "old=$(pidof audioserver || true)" in bridge
    assert 'new=$(pidof audioserver || true)' in bridge
    assert '\\"$new\\" != \\"$old\\"' in bridge
    assert "readBoundedProcessOutput(process.getInputStream())" in bridge
    assert 'clearError("Post-call audioserver recovery failed")' in bridge
    assert 'result.put("audioserver_recoveries", audioServerRecoveries.get());' in bridge
    assert 'result.put("audioserver_recovery_detail", audioServerRecoveryDetail);' in bridge
    assert "DigitalAudioBridge.onCellularCallEnded();" in calls
    assert 'Log.i(TAG, "Playout ACK stopped after the call ended")' in bridge
    assert 'Log.i(TAG, "Uplink client closed the completed playout stream")' in bridge


def test_call_end_during_partial_audio_write_is_normal_teardown() -> None:
    source = BRIDGE.read_text(encoding="utf-8")

    state_check = 'if (!"ACTIVE".equals(CallManager.getCallState())) return;'
    write_failure = '"Telephony playout write failed"'
    write_loop = source.index("while (writeOffset < pcm.length)")
    state_recheck = source.index(state_check, write_loop)
    failure_report = source.index(write_failure, write_loop)

    assert state_recheck < failure_report


def test_remote_tunnel_cannot_starve_playout_acknowledgements() -> None:
    source = REMOTE_LINK.read_text(encoding="utf-8")
    codec = PROTOCOL_CODEC.read_text(encoding="utf-8")

    # Capture, playout ACK, and control must use distinct v2 carrier sockets.
    # Fair locking alone cannot preempt a writer blocked in the kernel.
    assert "private static final int VERSION_V2 = 2;" in source
    assert "private final Map<Integer, V2Stream> v2Streams" in source
    assert "stream.relay = relay;" in source
    assert 'throw new IOException("v2 DATA is forbidden on the coordinator connection")' in source
    assert "new ReentrantLock(true)" in source
    assert "lock.lock();" in source
    assert "lock.unlock();" in source
    assert "public static synchronized void write" not in codec
    assert "public static void write" in codec


def test_v2_stream_timeout_budgets_are_ordered_across_phone_relay_and_runtime() -> None:
    android = REMOTE_LINK.read_text(encoding="utf-8")
    relay = PYTHON_REMOTE_LINK.read_text(encoding="utf-8")
    runtime = FRAMED_LINK.read_text(encoding="utf-8")

    assert "private static final int CONNECT_TIMEOUT_MS = 15_000;" in android
    assert "PHONE_STREAM_CONNECT_TIMEOUT_SECONDS = 15.0" in relay
    assert "V2_STREAM_ATTACH_TIMEOUT_SECONDS = 20.0" in relay
    assert "REMOTE_STREAM_HANDSHAKE_TIMEOUT_SECONDS = 25.0" in runtime


def test_phh_su_policy_is_command_scoped_and_installer_managed() -> None:
    provisioner = PHH_SU_PROVISIONER.read_text(encoding="utf-8")
    installer = (ROOT / "android_service_apk/build_and_install.sh").read_text(
        encoding="utf-8"
    )

    assert "old=$(pidof audioserver || true)" in provisioner
    assert 'new=$(pidof audioserver || true)' in provisioner
    assert "DELETE FROM uid_policy WHERE uid=$APP_UID;" in provisioner
    assert "INSERT INTO uid_policy (uid, policy, until, command)" in provisioner
    assert 'if [ "$POLICY" != "allow:$RECOVERY_COMMAND" ]' in provisioner
    assert 'provision_phh_su_audio_recovery.sh" "$DEV_ID"' in installer
