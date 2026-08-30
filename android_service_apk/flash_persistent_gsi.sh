#!/usr/bin/env bash
# One-time fastbootd deployment of a prevalidated persistent PhoneAgent GSI.

set -euo pipefail

usage() {
    echo "Usage: $0 --serial SERIAL --image PATH --rollback-image PATH --link-key PATH --commit"
    exit 2
}

SERIAL=""
IMAGE=""
ROLLBACK_IMAGE=""
LINK_KEY=""
COMMIT=false
while [ "$#" -gt 0 ]; do
    case "$1" in
        --serial) SERIAL="${2:-}"; shift 2 ;;
        --image) IMAGE="${2:-}"; shift 2 ;;
        --rollback-image) ROLLBACK_IMAGE="${2:-}"; shift 2 ;;
        --link-key) LINK_KEY="${2:-}"; shift 2 ;;
        --commit) COMMIT=true; shift ;;
        *) usage ;;
    esac
done
[ -n "$SERIAL" ] && [ -n "$IMAGE" ] && [ -n "$ROLLBACK_IMAGE" ] \
    && [ -n "$LINK_KEY" ] || usage
[ "$COMMIT" = true ] || usage
[ -f "$IMAGE" ] || { echo "[x] Image not found: $IMAGE"; exit 1; }
[ -f "$ROLLBACK_IMAGE" ] || { echo "[x] Rollback image not found: $ROLLBACK_IMAGE"; exit 1; }
[ -f "$LINK_KEY" ] || { echo "[x] Link key not found: $LINK_KEY"; exit 1; }
LINK_KEY_BYTES="$(wc -c < "$LINK_KEY" | tr -d ' ')"
[ "$LINK_KEY_BYTES" -ge 32 ] && [ "$LINK_KEY_BYTES" -le 4096 ] || {
    echo "[x] Link key must contain between 32 and 4096 bytes."
    exit 1
}

ADB="$(command -v adb)"
FASTBOOT="$(command -v fastboot)"
DEBUGFS="${DEBUGFS:-/opt/homebrew/opt/e2fsprogs/sbin/debugfs}"
E2FSCK="${E2FSCK:-/opt/homebrew/opt/e2fsprogs/sbin/e2fsck}"
[ -x "$DEBUGFS" ] && [ -x "$E2FSCK" ] || {
    echo "[x] Homebrew e2fsprogs tools are required."
    exit 1
}

IMAGE_SIZE="$(stat -f '%z' "$IMAGE")"
ROLLBACK_SIZE="$(stat -f '%z' "$ROLLBACK_IMAGE")"
[ "$IMAGE_SIZE" = "$ROLLBACK_SIZE" ] || {
    echo "[x] Image and rollback image sizes differ."
    exit 1
}
"$E2FSCK" -fn "$IMAGE" >/dev/null
"$DEBUGFS" -R 'stat /system/priv-app/PhoneAgentGateway/PhoneAgentGateway.apk' \
    "$IMAGE" 2>/dev/null | grep -q 'Mode:  0644' || {
    echo "[x] Image does not contain the validated PhoneAgent APK."
    exit 1
}
"$DEBUGFS" -R 'stat /system/etc/permissions/privapp-permissions-com.phoneagent.gateway.xml' \
    "$IMAGE" 2>/dev/null | grep -q 'Mode:  0644' || {
    echo "[x] Image does not contain the privileged permission allowlist."
    exit 1
}

"$ADB" -s "$SERIAL" get-state >/dev/null
FINGERPRINT="$("$ADB" -s "$SERIAL" shell getprop ro.build.fingerprint | tr -d '\r')"
BUILD_TYPE="$("$ADB" -s "$SERIAL" shell getprop ro.build.type | tr -d '\r')"
VERIFIED_STATE="$("$ADB" -s "$SERIAL" shell getprop ro.boot.verifiedbootstate | tr -d '\r')"
SLOT_SUFFIX="$("$ADB" -s "$SERIAL" shell getprop ro.boot.slot_suffix | tr -d '\r')"
case "$SLOT_SUFFIX" in _a|_b) ;; *) echo "[x] Unexpected slot: $SLOT_SUFFIX"; exit 1 ;; esac
echo "$FINGERPRINT" | grep -q 'tdgsi_arm64_ab' || {
    echo "[x] Refusing non-reviewed build: $FINGERPRINT"
    exit 1
}
[ "$BUILD_TYPE" = "userdebug" ] && [ "$VERIFIED_STATE" = "orange" ] || {
    echo "[x] Expected unlocked userdebug device; type=$BUILD_TYPE verified=$VERIFIED_STATE"
    exit 1
}
"$ADB" -s "$SERIAL" shell "su -c 'id; avbctl get-verity; avbctl get-verification'" \
    | grep -q 'verification is disabled' || {
    echo "[x] AVB verification is not disabled."
    exit 1
}

PARTITION="system${SLOT_SUFFIX}"
DEVICE_SIZE="$("$ADB" -s "$SERIAL" shell \
    "su -c 'blockdev --getsize64 /dev/block/mapper/$PARTITION'" | tr -d '\r')"
[ "$IMAGE_SIZE" = "$DEVICE_SIZE" ] || {
    echo "[x] Image size $IMAGE_SIZE does not match $PARTITION size $DEVICE_SIZE."
    exit 1
}
CALL_BLOCK="$("$ADB" -s "$SERIAL" shell dumpsys telecom \
    | sed -n '/mCalls:/,/mCallAudioManager:/p')"
[ "$(echo "$CALL_BLOCK" | wc -l | tr -d ' ')" -le 2 ] || {
    echo "[x] Refusing to flash while Telecom reports a call."
    exit 1
}
BATTERY="$("$ADB" -s "$SERIAL" shell dumpsys battery \
    | awk '/level:/ {print $2; exit}' | tr -d '\r')"
[ "${BATTERY:-0}" -ge 50 ] || { echo "[x] Battery below 50%."; exit 1; }

RECEIPT="$(dirname "$IMAGE")/flash-receipt-$(date +%Y%m%d-%H%M%S).txt"
{
    echo "serial=$SERIAL"
    echo "fingerprint=$FINGERPRINT"
    echo "partition=$PARTITION"
    echo "image_size=$IMAGE_SIZE"
    echo "image_sha256=$(shasum -a 256 "$IMAGE" | awk '{print $1}')"
    echo "rollback_image=$ROLLBACK_IMAGE"
    echo "rollback_sha256=$(shasum -a 256 "$ROLLBACK_IMAGE" | awk '{print $1}')"
} | tee "$RECEIPT"

echo "[*] Entering userspace fastbootd..."
"$ADB" -s "$SERIAL" reboot fastboot
for _ in $(seq 1 90); do
    if "$FASTBOOT" -s "$SERIAL" devices | grep -q "^$SERIAL"; then break; fi
    sleep 2
done
"$FASTBOOT" -s "$SERIAL" devices | grep -q "^$SERIAL" || {
    echo "[x] Device did not enter fastbootd."
    exit 1
}
"$FASTBOOT" -s "$SERIAL" getvar is-userspace 2>&1 | grep -q 'yes' || {
    echo "[x] Device is not in userspace fastbootd."
    exit 1
}
"$FASTBOOT" -s "$SERIAL" getvar "is-logical:$PARTITION" 2>&1 | grep -q 'yes' || {
    echo "[x] $PARTITION is not reported as a logical partition."
    exit 1
}

echo "[*] Flashing only $PARTITION..."
if ! "$FASTBOOT" -s "$SERIAL" flash "$PARTITION" "$IMAGE"; then
    echo "[x] Flash failed. The untouched rollback image is: $ROLLBACK_IMAGE"
    exit 1
fi
"$FASTBOOT" -s "$SERIAL" reboot

"$ADB" -s "$SERIAL" wait-for-device
for _ in $(seq 1 150); do
    if [ "$("$ADB" -s "$SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; then
        break
    fi
    sleep 2
done
[ "$("$ADB" -s "$SERIAL" shell getprop sys.boot_completed | tr -d '\r')" = "1" ] || {
    echo "[x] Android did not complete boot. Roll back from fastbootd with:"
    echo "    fastboot -s $SERIAL flash $PARTITION $ROLLBACK_IMAGE"
    exit 1
}

echo "[*] Performing one-time user-0 and dialer provisioning..."
# Remove only this package's recoverable /data/app update. This makes Package
# Manager fall back to the just-flashed /system APK and turns the subsequent
# reboot test into a real persistence proof rather than an update-layer proof.
CURRENT_PACKAGE_PATH="$("$ADB" -s "$SERIAL" shell \
    'pm path com.phoneagent.gateway' | tr -d '\r')"
if echo "$CURRENT_PACKAGE_PATH" | grep -q '^package:/data/app/'; then
    # This reviewed ROM may return non-zero even after successfully removing
    # the update, so validate the resulting package path instead of trusting
    # the command's exit status.
    "$ADB" -s "$SERIAL" shell \
        'pm uninstall-system-updates com.phoneagent.gateway' >/dev/null || true
fi
for _ in $(seq 1 20); do
    CURRENT_PACKAGE_PATH="$("$ADB" -s "$SERIAL" shell \
        'pm path com.phoneagent.gateway' | tr -d '\r')"
    if echo "$CURRENT_PACKAGE_PATH" \
        | grep -q '^package:/system/priv-app/PhoneAgentGateway/PhoneAgentGateway.apk$'; then
        break
    fi
    sleep 1
done
echo "$CURRENT_PACKAGE_PATH" \
    | grep -q '^package:/system/priv-app/PhoneAgentGateway/PhoneAgentGateway.apk$' || {
        echo "[x] PhoneAgent did not fall back to the flashed system APK."
        exit 1
    }
"$ADB" -s "$SERIAL" shell 'cmd package install-existing --user 0 com.phoneagent.gateway' \
    >/dev/null 2>&1 || true
for permission in \
    android.permission.CALL_PHONE \
    android.permission.READ_PHONE_STATE \
    android.permission.RECORD_AUDIO \
    android.permission.READ_CALL_LOG \
    android.permission.WRITE_CALL_LOG \
    android.permission.POST_NOTIFICATIONS; do
    "$ADB" -s "$SERIAL" shell "pm grant com.phoneagent.gateway $permission" \
        >/dev/null 2>&1 || true
done
"$ADB" -s "$SERIAL" shell \
    'cmd role add-role-holder android.app.role.DIALER com.phoneagent.gateway 0'

REMOTE_KEY="/data/local/tmp/phoneagent-link-key"
"$ADB" -s "$SERIAL" push "$LINK_KEY" "$REMOTE_KEY" >/dev/null
APP_UID="$("$ADB" -s "$SERIAL" shell dumpsys package com.phoneagent.gateway \
    | awk -F= '/userId=|appId=/{gsub(/\r/, "", $2); gsub(/ /, "", $2); print $2; exit}')"
[ -n "$APP_UID" ] || { echo "[x] Could not resolve PhoneAgent app UID."; exit 1; }
"$ADB" -s "$SERIAL" shell "su -c '
    mkdir -p /data/user/0/com.phoneagent.gateway/files &&
    cp $REMOTE_KEY /data/user/0/com.phoneagent.gateway/files/link.key &&
    chown $APP_UID:$APP_UID /data/user/0/com.phoneagent.gateway/files/link.key &&
    chmod 0600 /data/user/0/com.phoneagent.gateway/files/link.key &&
    restorecon -R /data/user/0/com.phoneagent.gateway/files &&
    rm -f $REMOTE_KEY
'"
LOCAL_KEY_HASH="$(shasum -a 256 "$LINK_KEY" | awk '{print $1}')"
PHONE_KEY_HASH="$("$ADB" -s "$SERIAL" shell \
    "su -c 'sha256sum /data/user/0/com.phoneagent.gateway/files/link.key'" \
    | awk '{gsub(/\r/, "", $1); print $1}')"
[ "$LOCAL_KEY_HASH" = "$PHONE_KEY_HASH" ] || {
    echo "[x] Provisioned link-key hash mismatch."
    exit 1
}
"$ADB" -s "$SERIAL" shell \
    'am start-foreground-service -n com.phoneagent.gateway/.GatewayService' >/dev/null
"$ADB" -s "$SERIAL" forward tcp:8765 tcp:8765
"$ADB" -s "$SERIAL" forward tcp:8766 tcp:8766
"$ADB" -s "$SERIAL" forward tcp:8767 tcp:8767
"$ADB" -s "$SERIAL" forward tcp:8768 tcp:8768

PACKAGE_DUMP="$("$ADB" -s "$SERIAL" shell dumpsys package com.phoneagent.gateway)"
echo "$PACKAGE_DUMP" | grep -q 'codePath=/system/priv-app/PhoneAgentGateway' || exit 1
for permission in CAPTURE_AUDIO_OUTPUT MODIFY_AUDIO_ROUTING MODIFY_PHONE_STATE CONTROL_INCALL_EXPERIENCE; do
    echo "$PACKAGE_DUMP" | grep -q "android.permission.$permission: granted=true" || exit 1
done
"$ADB" -s "$SERIAL" shell 'cmd role get-role-holders android.app.role.DIALER 0' \
    | tr -d '\r' | grep -q '^com.phoneagent.gateway$' || exit 1
echo "[+] Persistent system image installed and provisioned. Receipt: $RECEIPT"
echo "[i] Untouched rollback image: $ROLLBACK_IMAGE"
