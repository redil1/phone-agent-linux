"""Guards for immutable Android runtime provenance."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "android_service_apk/build_and_install.sh"
HTTP_SERVER = ROOT / "android_service_apk/src/com/phoneagent/gateway/HttpServerEngine.java"
CONTROL_SERVER = (
    ROOT / "android_service_apk/src/com/phoneagent/gateway/ProtocolControlServer.java"
)


def test_apk_build_embeds_a_location_independent_source_digest() -> None:
    source = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "ANDROID_SOURCE_SHA256=" in source
    assert "find src res libs -type f -print" in source
    assert "LC_ALL=C sort" in source
    assert "SHA256_COMMAND=(sha256sum)" in source
    assert "SHA256_COMMAND=(shasum -a 256)" in source
    assert 'static final String ANDROID_SOURCE_SHA256 = "$ANDROID_SOURCE_SHA256";' in source
    assert 'SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1700000000}"' in source
    assert 'os.utime(sys.argv[2], (timestamp, timestamp))' in source
    assert "--v1-signing-enabled false" in source


def test_apk_install_is_fail_closed_and_attested() -> None:
    source = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "set -euo pipefail" in source
    assert "Exactly one authorized Android device is required" in source
    assert "--device-id" in source
    assert 'BUILD_TOOLS_VERSION="${ANDROID_BUILD_TOOLS_VERSION:-34.0.0}"' in source
    assert 'PLATFORM_VERSION="${ANDROID_PLATFORM_VERSION:-34}"' in source
    assert "previous.apk" in source
    assert 'KEYSTORE="${PHONE_AGENT_SIGNING_KEYSTORE:-$DIR/debug.keystore}"' in source
    assert 'if [ "$PREVIOUS_SIGNER_CERT_SHA256" != "$LOCAL_SIGNER_CERT_SHA256" ]' in source
    assert "Candidate signer does not match the installed privileged app" in source
    assert "install-receipt.json" in source
    assert 'if [ "$INSTALLED_APK_SHA256" != "$LOCAL_APK_SHA256" ]' in source
    assert 'if [ "$HEALTH_SOURCE_SHA256" != "$ANDROID_SOURCE_SHA256" ]' in source
    assert 'if [ "$HEALTH_PROTOCOL_VERSION" != "2" ]' in source
    for permission in (
        "CAPTURE_AUDIO_OUTPUT",
        "MODIFY_AUDIO_ROUTING",
        "MODIFY_PHONE_STATE",
        "CONTROL_INCALL_EXPERIENCE",
    ):
        assert permission in source


def test_both_health_protocols_report_apk_source_and_remote_link_version() -> None:
    for source_path in (HTTP_SERVER, CONTROL_SERVER):
        source = source_path.read_text(encoding="utf-8")
        assert '"apk_source_sha256"' in source
        assert "BuildProvenance.ANDROID_SOURCE_SHA256" in source
        assert '"remote_link_protocol_version"' in source
        assert "BuildProvenance.REMOTE_LINK_PROTOCOL_VERSION" in source
        assert '"remote_link_negotiated_version"' in source
        assert "GatewayService.remoteLinkProtocolVersion()" in source
