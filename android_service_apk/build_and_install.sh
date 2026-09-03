#!/usr/bin/env bash
# Build, Package, Sign, and Install PhoneAgent Native Headless Service APK

set -euo pipefail

BUILD_ONLY=false
DEVICE_ID="${PHONE_AGENT_DEVICE_ID:-${ANDROID_SERIAL:-}}"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --build-only)
            BUILD_ONLY=true
            shift
            ;;
        --device-id)
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                echo "[x] --device-id requires a non-empty serial."
                exit 2
            fi
            DEVICE_ID="$2"
            shift 2
            ;;
        *)
            echo "Usage: $0 [--build-only] [--device-id SERIAL]"
            exit 2
            ;;
    esac
done

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}}"
if [ ! -d "$SDK" ] && [ -d "/Users/aziz/Library/Android/sdk" ]; then
    SDK="/Users/aziz/Library/Android/sdk"
fi
BUILD_TOOLS_VERSION="${ANDROID_BUILD_TOOLS_VERSION:-34.0.0}"
PLATFORM_VERSION="${ANDROID_PLATFORM_VERSION:-34}"
BT_DIR="$SDK/build-tools/$BUILD_TOOLS_VERSION"
PLAT_DIR="$SDK/platforms/android-$PLATFORM_VERSION"

AAPT2="$BT_DIR/aapt2"
D8="$BT_DIR/d8"
ZIPALIGN="$BT_DIR/zipalign"
APKSIGNER="$BT_DIR/apksigner"
ANDROID_JAR="$PLAT_DIR/android.jar"

for required_tool in "$AAPT2" "$D8" "$ZIPALIGN" "$APKSIGNER"; do
    if [ ! -x "$required_tool" ]; then
        echo "[x] Required Android build tool is missing: $required_tool"
        exit 1
    fi
done
if [ ! -f "$ANDROID_JAR" ]; then
    echo "[x] Required Android platform is missing: $ANDROID_JAR"
    exit 1
fi

BUILD_DIR="$DIR/build"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1700000000}"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/gen" "$BUILD_DIR/classes" "$BUILD_DIR/res_compiled"

echo "============================================================"
echo "   Compiling PhoneAgent Native Headless Gateway APK        "
echo "============================================================"

# 1. Compile Resources with AAPT2
echo "[*] Compiling resources with AAPT2..."
"$AAPT2" compile --dir "$DIR/res" -o "$BUILD_DIR/res_compiled/"

echo "[*] Linking resources and generating R.java..."
"$AAPT2" link \
    -I "$ANDROID_JAR" \
    --manifest "$DIR/AndroidManifest.xml" \
    --java "$BUILD_DIR/gen" \
    -o "$BUILD_DIR/unaligned.apk" \
    "$BUILD_DIR/res_compiled"/*.flat

# Bind the APK to the exact Android source tree that produced it. Runtime
# health exposes this value so a server can distinguish an upgraded handset
# from an older binary even when Android's versionName has not changed. Use
# relative paths and a bytewise-sorted manifest to keep the digest independent
# of the checkout location and reproducible across Linux and macOS.
if command -v sha256sum >/dev/null 2>&1; then
    SHA256_COMMAND=(sha256sum)
elif command -v shasum >/dev/null 2>&1; then
    SHA256_COMMAND=(shasum -a 256)
else
    echo "[x] Neither sha256sum nor shasum is available."
    exit 1
fi
ANDROID_SOURCE_SHA256="$({
    cd "$DIR"
    {
        printf '%s\n' AndroidManifest.xml
        find src res libs -type f -print
    } | LC_ALL=C sort | while IFS= read -r source_file; do
        source_digest="$("${SHA256_COMMAND[@]}" "$source_file" | awk '{print $1}')"
        printf '%s  %s\n' "$source_digest" "$source_file"
    done
} | "${SHA256_COMMAND[@]}" | awk '{print $1}')"
PROVENANCE_DIR="$BUILD_DIR/gen/com/phoneagent/gateway"
mkdir -p "$PROVENANCE_DIR"
cat > "$PROVENANCE_DIR/BuildProvenance.java" <<EOF
package com.phoneagent.gateway;

final class BuildProvenance {
    static final String ANDROID_SOURCE_SHA256 = "$ANDROID_SOURCE_SHA256";
    static final int REMOTE_LINK_PROTOCOL_VERSION = 2;
    private BuildProvenance() {}
}
EOF
echo "[*] Android source SHA-256: $ANDROID_SOURCE_SHA256"

# 2. Compile Java Source Files with javac
echo "[*] Compiling Java source files with javac..."
# The QR decoder is vendored rather than fetched at build time so the APK is
# reproducible offline. It is pure Java with no Android dependencies.
LIBS="$(find "$DIR/libs" -name '*.jar' 2>/dev/null | tr '\n' ':')"
javac --release 17 -encoding UTF-8 \
    -cp "$ANDROID_JAR:$LIBS" \
    -d "$BUILD_DIR/classes" \
    $(find "$DIR/src" "$BUILD_DIR/gen" -name "*.java")

# 3. Convert .class to .dex with D8
echo "[*] Converting bytecode to classes.dex with D8..."
"$D8" --output "$BUILD_DIR" \
    --lib "$ANDROID_JAR" \
    --min-api 28 \
    $(find "$DIR/libs" -name '*.jar' 2>/dev/null) \
    $(find "$BUILD_DIR/classes" -name "*.class")

# 4. Add classes.dex into unaligned APK
echo "[*] Adding classes.dex to APK..."
# jar records the input mtime in the ZIP central directory. Pin it so two
# builds of identical source do not produce different APK bytes.
python3 - "$SOURCE_DATE_EPOCH" "$BUILD_DIR/classes.dex" <<'PY'
import os
import sys

timestamp = int(sys.argv[1])
os.utime(sys.argv[2], (timestamp, timestamp))
PY
cd "$BUILD_DIR"
jar -uf "$BUILD_DIR/unaligned.apk" classes.dex
cd "$DIR"

# 5. Zipalign APK
echo "[*] Aligning APK with zipalign..."
"$ZIPALIGN" -f -v 4 "$BUILD_DIR/unaligned.apk" "$BUILD_DIR/aligned.apk" >/dev/null

# 6. Select Keystore and Sign APK
KEYSTORE="${PHONE_AGENT_SIGNING_KEYSTORE:-$DIR/debug.keystore}"
KEY_ALIAS="${PHONE_AGENT_SIGNING_ALIAS:-debug}"
KEYSTORE_PASSWORD="${PHONE_AGENT_SIGNING_STORE_PASSWORD:-android}"
KEY_PASSWORD="${PHONE_AGENT_SIGNING_KEY_PASSWORD:-$KEYSTORE_PASSWORD}"
if [ ! -f "$KEYSTORE" ]; then
    if [ -n "${PHONE_AGENT_SIGNING_KEYSTORE:-}" ]; then
        echo "[x] PHONE_AGENT_SIGNING_KEYSTORE does not exist: $KEYSTORE"
        exit 1
    fi
    echo "[*] Generating debug signing keystore..."
    keytool -genkey -v -keystore "$KEYSTORE" -alias "$KEY_ALIAS" -keyalg RSA -keysize 2048 \
        -validity 10000 -storepass "$KEYSTORE_PASSWORD" -keypass "$KEY_PASSWORD" \
        -dname "CN=PhoneAgent, OU=AI, O=Agent, L=Local, S=State, C=US"
fi

echo "[*] Signing APK with apksigner..."
FINAL_APK="$DIR/PhoneAgentGateway.apk"
"$APKSIGNER" sign \
    --v1-signing-enabled false \
    --v2-signing-enabled true \
    --v3-signing-enabled true \
    --ks "$KEYSTORE" \
    --ks-pass "pass:$KEYSTORE_PASSWORD" \
    --key-pass "pass:$KEY_PASSWORD" \
    --ks-key-alias "$KEY_ALIAS" \
    --out "$FINAL_APK" \
    "$BUILD_DIR/aligned.apk"

echo "[✓] APK Built & Signed: $FINAL_APK"

if [ "$BUILD_ONLY" = true ]; then
    echo "[✓] Build-only requested; device installation skipped."
    exit 0
fi

# 7. Install to exactly one authorized Android device.
if [ -n "$DEVICE_ID" ]; then
    DEV_ID="$DEVICE_ID"
    if [ "$(adb -s "$DEV_ID" get-state 2>/dev/null || true)" != "device" ]; then
        echo "[x] Requested Android device is not connected and authorized."
        exit 1
    fi
else
    CONNECTED_DEVICES=$(adb devices | awk 'NR > 1 && $2 == "device" {print $1}')
    DEVICE_COUNT=$(printf '%s\n' "$CONNECTED_DEVICES" | awk 'NF {count++} END {print count + 0}')
    if [ "$DEVICE_COUNT" -ne 1 ]; then
        echo "[x] Exactly one authorized Android device is required; found $DEVICE_COUNT."
        echo "    Use --device-id or PHONE_AGENT_DEVICE_ID when more than one device is attached."
        exit 1
    fi
    DEV_ID="$CONNECTED_DEVICES"
fi

PACKAGE_NAME="com.phoneagent.gateway"
LOCAL_APK_SHA256="$("${SHA256_COMMAND[@]}" "$FINAL_APK" | awk '{print $1}')"
LOCAL_SIGNER_CERT_SHA256=$("$APKSIGNER" verify --print-certs "$FINAL_APK" \
    | awk -F': ' '/Signer #1 certificate SHA-256 digest/ {print $2; exit}')
if [ -z "$LOCAL_SIGNER_CERT_SHA256" ]; then
    echo "[x] Could not determine the candidate APK signing certificate."
    exit 1
fi
INSTALL_ID="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_ROOT="${PHONE_AGENT_APK_BACKUP_DIR:-$DIR/device_backups}"
BACKUP_DIR="$BACKUP_ROOT/$INSTALL_ID"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_ROOT" "$BACKUP_DIR"

PREVIOUS_PACKAGE_PATH=$(adb -s "$DEV_ID" shell "pm path $PACKAGE_NAME" 2>/dev/null \
    | tr -d '\r' | sed -n 's/^package://p' | head -n1 || true)
PREVIOUS_APK_SHA256=""
PREVIOUS_SIGNER_CERT_SHA256=""
if [ -n "$PREVIOUS_PACKAGE_PATH" ]; then
    echo "[*] Backing up the currently installed APK before replacement..."
    adb -s "$DEV_ID" pull "$PREVIOUS_PACKAGE_PATH" "$BACKUP_DIR/previous.apk" >/dev/null
    PREVIOUS_APK_SHA256="$("${SHA256_COMMAND[@]}" "$BACKUP_DIR/previous.apk" | awk '{print $1}')"
    PREVIOUS_SIGNER_CERT_SHA256=$("$APKSIGNER" verify --print-certs \
        "$BACKUP_DIR/previous.apk" \
        | awk -F': ' '/Signer #1 certificate SHA-256 digest/ {print $2; exit}')
    if [ -z "$PREVIOUS_SIGNER_CERT_SHA256" ]; then
        echo "[x] Could not determine the installed APK signing certificate."
        exit 1
    fi
    if [ "$PREVIOUS_SIGNER_CERT_SHA256" != "$LOCAL_SIGNER_CERT_SHA256" ]; then
        echo "[x] Candidate signer does not match the installed privileged app; refusing update."
        echo "    Select its original key with PHONE_AGENT_SIGNING_KEYSTORE."
        exit 1
    fi
fi
if adb -s "$DEV_ID" shell \
    "test -f /system/etc/permissions/privapp-permissions-com.phoneagent.gateway.xml" \
    >/dev/null 2>&1; then
    adb -s "$DEV_ID" pull \
        /system/etc/permissions/privapp-permissions-com.phoneagent.gateway.xml \
        "$BACKUP_DIR/previous-privapp-permissions.xml" >/dev/null
fi

if [ -n "$DEV_ID" ]; then
    echo "[*] Installing APK to the selected authorized device..."
    adb -s "$DEV_ID" install -r -g "$FINAL_APK"

    "$DIR/provision_phh_su_audio_recovery.sh" "$DEV_ID"

    # Installing force-stops the gateway. A SIGKILL never runs onDestroy, so any
    # telephony AudioTrack that was live at that moment stays registered inside
    # AudioFlinger and permanently consumes one of the telephony output's limited
    # track slots. Enough of those and no injection track can be created again,
    # which presents as a call where the remote party simply hears nothing.
    # Restarting audioserver releases every orphan; init brings it straight back.
    echo "[*] Releasing orphaned telephony audio tracks (restarting audioserver)..."
    adb -s "$DEV_ID" shell "su -c 'killall audioserver'" >/dev/null 2>&1 || true
    sleep 3

    echo "[*] Granting system & telephony permissions..."
    adb -s "$DEV_ID" shell "pm grant $PACKAGE_NAME android.permission.CALL_PHONE" 2>/dev/null || true
    adb -s "$DEV_ID" shell "pm grant $PACKAGE_NAME android.permission.READ_PHONE_STATE" 2>/dev/null || true
    adb -s "$DEV_ID" shell "pm grant $PACKAGE_NAME android.permission.RECORD_AUDIO" 2>/dev/null || true
    adb -s "$DEV_ID" shell "pm grant $PACKAGE_NAME android.permission.READ_CALL_LOG" 2>/dev/null || true
    adb -s "$DEV_ID" shell "pm grant $PACKAGE_NAME android.permission.POST_NOTIFICATIONS" 2>/dev/null || true

    echo "[*] Assigning PhoneAgent as the Android default dialer..."
    adb -s "$DEV_ID" shell "cmd role add-role-holder android.app.role.DIALER $PACKAGE_NAME 0"
    ROLE_HOLDERS=$(adb -s "$DEV_ID" shell "cmd role get-role-holders android.app.role.DIALER 0" | tr -d '\r')
    if ! echo "$ROLE_HOLDERS" | grep -q "^$PACKAGE_NAME$"; then
        echo "[✗] ROLE_DIALER assignment failed. Open the PhoneAgent app and grant the role manually."
        exit 1
    fi

    echo "[*] Stopping the legacy shell listener to free control port 8765..."
    adb -s "$DEV_ID" shell "su -c 'pkill -f \"[r]oot_gateway.sh\" 2>/dev/null || true; pkill -f \"[n]etcat -L -p 8765\" 2>/dev/null || true'" || true

    echo "[*] Starting Headless Gateway Service..."
    adb -s "$DEV_ID" shell "am start-foreground-service -n $PACKAGE_NAME/.GatewayService"

    echo "[*] Setting up ADB port forwarding..."
    adb -s "$DEV_ID" forward tcp:8765 tcp:8765
    adb -s "$DEV_ID" forward tcp:8766 tcp:8766
    adb -s "$DEV_ID" forward tcp:8767 tcp:8767
    adb -s "$DEV_ID" forward tcp:8768 tcp:8768

    sleep 1
    echo "[*] Verifying Headless Service Health..."
    STATUS=$(curl -fsS http://localhost:8765/health || true)
    if ! echo "$STATUS" | grep -q '"gateway":"ready"'; then
        echo "[✗] Native gateway health check failed: ${STATUS:-no response}"
        exit 1
    fi

    INSTALLED_PACKAGE_PATH=$(adb -s "$DEV_ID" shell "pm path $PACKAGE_NAME" \
        | tr -d '\r' | sed -n 's/^package://p' | head -n1)
    if [ -z "$INSTALLED_PACKAGE_PATH" ]; then
        echo "[x] PackageManager did not return the installed APK path."
        exit 1
    fi
    adb -s "$DEV_ID" pull "$INSTALLED_PACKAGE_PATH" "$BUILD_DIR/installed.apk" >/dev/null
    INSTALLED_APK_SHA256="$("${SHA256_COMMAND[@]}" "$BUILD_DIR/installed.apk" | awk '{print $1}')"
    if [ "$INSTALLED_APK_SHA256" != "$LOCAL_APK_SHA256" ]; then
        echo "[x] Installed APK bytes do not match the built candidate."
        exit 1
    fi

    PACKAGE_DUMP=$(adb -s "$DEV_ID" shell "dumpsys package $PACKAGE_NAME")
    if ! echo "$PACKAGE_DUMP" | grep -q 'SYSTEM' \
        || ! echo "$PACKAGE_DUMP" | grep -q 'PRIVILEGED'; then
        echo "[x] Installed package is not an updated privileged system application."
        exit 1
    fi
    for privileged_permission in \
        CAPTURE_AUDIO_OUTPUT \
        MODIFY_AUDIO_ROUTING \
        MODIFY_PHONE_STATE \
        CONTROL_INCALL_EXPERIENCE; do
        if ! echo "$PACKAGE_DUMP" \
            | grep -q "android.permission.$privileged_permission: granted=true"; then
            echo "[x] Required privileged permission is missing: $privileged_permission"
            exit 1
        fi
    done

    HEALTH_SOURCE_SHA256=$(printf '%s' "$STATUS" | python3 -c \
        'import json, sys; print(json.load(sys.stdin).get("apk_source_sha256", ""))')
    HEALTH_PROTOCOL_VERSION=$(printf '%s' "$STATUS" | python3 -c \
        'import json, sys; print(json.load(sys.stdin).get("remote_link_protocol_version", ""))')
    HEALTH_NEGOTIATED_VERSION=$(printf '%s' "$STATUS" | python3 -c \
        'import json, sys; print(json.load(sys.stdin).get("remote_link_negotiated_version", ""))')
    if [ "$HEALTH_SOURCE_SHA256" != "$ANDROID_SOURCE_SHA256" ]; then
        echo "[x] Running gateway source provenance does not match the built candidate."
        exit 1
    fi
    if [ "$HEALTH_PROTOCOL_VERSION" != "2" ]; then
        echo "[x] Running gateway does not advertise remote-link protocol v2."
        exit 1
    fi

    DEVICE_SERIAL_SHA256=$(printf '%s' "$DEV_ID" | "${SHA256_COMMAND[@]}" | awk '{print $1}')
    python3 - \
        "$BACKUP_DIR/install-receipt.json" \
        "$INSTALL_ID" \
        "$DEVICE_SERIAL_SHA256" \
        "$PREVIOUS_PACKAGE_PATH" \
        "$PREVIOUS_APK_SHA256" \
        "$PREVIOUS_SIGNER_CERT_SHA256" \
        "$INSTALLED_PACKAGE_PATH" \
        "$INSTALLED_APK_SHA256" \
        "$ANDROID_SOURCE_SHA256" \
        "$LOCAL_SIGNER_CERT_SHA256" \
        "$HEALTH_NEGOTIATED_VERSION" <<'PY'
import json
import sys

(
    output,
    install_id,
    serial_hash,
    previous_path,
    previous_hash,
    previous_signer_hash,
    installed_path,
    installed_hash,
    source_hash,
    signer_hash,
    negotiated_version,
) = sys.argv[1:]
receipt = {
    "schema_version": 1,
    "install_id": install_id,
    "device_serial_sha256": serial_hash,
    "previous_package_path": previous_path,
    "previous_apk_sha256": previous_hash,
    "previous_signer_certificate_sha256": previous_signer_hash,
    "installed_package_path": installed_path,
    "installed_apk_sha256": installed_hash,
    "apk_source_sha256": source_hash,
    "signer_certificate_sha256": signer_hash,
    "remote_link_protocol_version": 2,
    "remote_link_negotiated_version_at_install": int(negotiated_version or 0),
}
with open(output, "w", encoding="utf-8") as stream:
    json.dump(receipt, stream, indent=2, sort_keys=True)
    stream.write("\n")
PY
    chmod 600 "$BACKUP_DIR/install-receipt.json"
    echo "[✓] Health Status: $STATUS"
    echo "[✓] Installed APK SHA-256: $INSTALLED_APK_SHA256"
    echo "[✓] Rollback receipt: $BACKUP_DIR/install-receipt.json"

    echo "============================================================"
    echo "  NATIVE PHONEAGENT CONTROL & AUDIO SERVERS ARE ONLINE     "
    echo "============================================================"
fi
