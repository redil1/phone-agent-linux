#!/usr/bin/env bash
# Install PhoneAgent into the live userdebug overlay as a privileged appliance.
# This intentionally requires an explicit --commit because it changes /system
# overlay state and restarts the Android framework. Re-run after a full reboot;
# production persistence requires baking the same files into the GSI image.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APK="$DIR/PhoneAgentGateway.apk"
ALLOWLIST="$DIR/privapp-permissions-com.phoneagent.gateway.xml"

if [ "${1:-}" != "--commit" ]; then
    echo "Usage: $0 --commit"
    echo "Build the APK first. This writes a live priv-app overlay and restarts Android."
    exit 2
fi

DEV_ID=$(adb devices | awk '$2 == "device" {print $1; exit}')
if [ -z "$DEV_ID" ]; then
    echo "[✗] No ADB device is connected."
    exit 1
fi
if [ ! -f "$APK" ] || [ ! -f "$ALLOWLIST" ]; then
    echo "[✗] Build output or permission allowlist is missing."
    exit 1
fi

SDK=$(adb -s "$DEV_ID" shell getprop ro.build.version.sdk | tr -d '\r')
BUILD_TYPE=$(adb -s "$DEV_ID" shell getprop ro.build.type | tr -d '\r')
ROOT_ID=$(adb -s "$DEV_ID" shell "su -c id" 2>/dev/null || true)
if [ "$SDK" != "34" ] || [ "$BUILD_TYPE" != "userdebug" ] || ! echo "$ROOT_ID" | grep -q 'uid=0'; then
    echo "[✗] Expected the reviewed Android 14 userdebug device with working root."
    echo "    sdk=$SDK build_type=$BUILD_TYPE root=$ROOT_ID"
    exit 1
fi

wait_for_boot() {
    adb -s "$DEV_ID" wait-for-device
    for _ in $(seq 1 120); do
        if [ "$(adb -s "$DEV_ID" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; then
            return 0
        fi
        sleep 2
    done
    echo "[✗] Android did not finish booting in time."
    return 1
}

echo "[*] Restarting adbd as root and enabling overlayfs remount..."
adb -s "$DEV_ID" root
adb -s "$DEV_ID" wait-for-device
REMOUNT_OUTPUT=$(adb -s "$DEV_ID" remount 2>&1)
echo "$REMOUNT_OUTPUT"
if ! adb -s "$DEV_ID" shell "cat /proc/1/mountinfo" | grep -qE ' /system .* - overlay '; then
    echo "[*] Rebooting once to activate overlayfs, then remounting again..."
    adb -s "$DEV_ID" reboot
    wait_for_boot
    adb -s "$DEV_ID" root
    adb -s "$DEV_ID" wait-for-device
    adb -s "$DEV_ID" remount
fi

if adb -s "$DEV_ID" shell pm path com.phoneagent.gateway 2>/dev/null | grep -q '^package:/data/app/'; then
    echo "[*] Removing the development user-app copy before the first privileged scan..."
    adb -s "$DEV_ID" uninstall com.phoneagent.gateway >/dev/null 2>&1 || true
fi

echo "[*] Installing the privileged APK and permission allowlist..."
adb -s "$DEV_ID" shell "mkdir -p /system/priv-app/PhoneAgentGateway"
adb -s "$DEV_ID" push "$APK" /system/priv-app/PhoneAgentGateway/PhoneAgentGateway.apk
adb -s "$DEV_ID" push "$ALLOWLIST" /system/etc/permissions/privapp-permissions-com.phoneagent.gateway.xml
adb -s "$DEV_ID" shell "chmod 0644 /system/priv-app/PhoneAgentGateway/PhoneAgentGateway.apk /system/etc/permissions/privapp-permissions-com.phoneagent.gateway.xml"
adb -s "$DEV_ID" shell "chown root:root /system/priv-app/PhoneAgentGateway/PhoneAgentGateway.apk /system/etc/permissions/privapp-permissions-com.phoneagent.gateway.xml"

echo "[*] Restarting the Android framework so PackageManager scans the live overlay..."
adb -s "$DEV_ID" shell stop
sleep 2
adb -s "$DEV_ID" shell start
adb -s "$DEV_ID" wait-for-device
for _ in $(seq 1 120); do
    if adb -s "$DEV_ID" shell pm path com.phoneagent.gateway 2>/dev/null | grep -q '/system/priv-app/PhoneAgentGateway/'; then
        break
    fi
    sleep 2
done
if ! adb -s "$DEV_ID" shell pm path com.phoneagent.gateway 2>/dev/null | grep -q '/system/priv-app/PhoneAgentGateway/'; then
    echo "[✗] PackageManager did not register the live privileged overlay."
    exit 1
fi
for _ in $(seq 1 30); do
    adb -s "$DEV_ID" shell "cmd package install-existing --user 0 com.phoneagent.gateway" >/dev/null 2>&1 || true
    if adb -s "$DEV_ID" shell "pm list packages --user 0 com.phoneagent.gateway" | grep -q '^package:com.phoneagent.gateway$'; then
        break
    fi
    sleep 2
done
if ! adb -s "$DEV_ID" shell "pm list packages --user 0 com.phoneagent.gateway" | grep -q '^package:com.phoneagent.gateway$'; then
    echo "[✗] Privileged package was not enabled for Android user 0."
    exit 1
fi

echo "[*] Granting runtime permissions and ROLE_DIALER..."
for permission in \
    android.permission.CALL_PHONE \
    android.permission.READ_PHONE_STATE \
    android.permission.RECORD_AUDIO \
    android.permission.READ_CALL_LOG \
    android.permission.WRITE_CALL_LOG \
    android.permission.POST_NOTIFICATIONS; do
    adb -s "$DEV_ID" shell "pm grant com.phoneagent.gateway $permission" 2>/dev/null || true
done
for _ in $(seq 1 12); do
    adb -s "$DEV_ID" shell "cmd role add-role-holder android.app.role.DIALER com.phoneagent.gateway 0" >/dev/null 2>&1 || true
    if adb -s "$DEV_ID" shell "cmd role get-role-holders android.app.role.DIALER 0" | tr -d '\r' | grep -q '^com.phoneagent.gateway$'; then
        break
    fi
    sleep 2
done
if ! adb -s "$DEV_ID" shell "cmd role get-role-holders android.app.role.DIALER 0" | tr -d '\r' | grep -q '^com.phoneagent.gateway$'; then
    echo "[✗] ROLE_DIALER could not be assigned after framework restart."
    exit 1
fi

# A reboot discards the /system overlay and silently restores the APK baked into
# the GSI. Every check below still passed against that stale build, so the only
# reliable proof is that the bytes on the device are the bytes just built.
echo "[*] Verifying the installed APK is the one just built..."
DEVICE_APK_HASH=$(adb -s "$DEV_ID" shell md5sum /system/priv-app/PhoneAgentGateway/PhoneAgentGateway.apk 2>/dev/null | awk '{print $1}' | tr -d '\r')
LOCAL_APK_HASH=$(md5 -q "$APK" 2>/dev/null || md5sum "$APK" | awk '{print $1}')
if [ -z "$DEVICE_APK_HASH" ] || [ "$DEVICE_APK_HASH" != "$LOCAL_APK_HASH" ]; then
    echo "[✗] The device is not running the APK that was just built."
    echo "    device=${DEVICE_APK_HASH:-<unreadable>} built=${LOCAL_APK_HASH}"
    echo "    The /system overlay was probably discarded by a reboot. Re-run this script."
    exit 1
fi
echo "[✓] APK hash matches: ${LOCAL_APK_HASH}"

echo "[*] Verifying effective privileged permissions..."
PACKAGE_DUMP=$(adb -s "$DEV_ID" shell dumpsys package com.phoneagent.gateway)
for permission in CAPTURE_AUDIO_OUTPUT MODIFY_AUDIO_ROUTING MODIFY_PHONE_STATE; do
    if ! echo "$PACKAGE_DUMP" | grep -E "android.permission.$permission: granted=true" >/dev/null; then
        echo "[✗] android.permission.$permission was not granted after privileged install."
        exit 1
    fi
done

adb -s "$DEV_ID" shell "su -c 'pkill -f \"[r]oot_gateway.sh\" 2>/dev/null || true; pkill -f \"[n]etcat -L -p 8765\" 2>/dev/null || true'" || true
START_OUTPUT=""
for _ in $(seq 1 20); do
    START_OUTPUT=$(adb -s "$DEV_ID" shell "am start-foreground-service -n com.phoneagent.gateway/.GatewayService" 2>&1 || true)
    if ! echo "$START_OUTPUT" | grep -q '^Error:'; then
        echo "$START_OUTPUT"
        break
    fi
    sleep 2
done
if echo "$START_OUTPUT" | grep -q '^Error:'; then
    echo "[✗] Gateway service resolver did not become ready: $START_OUTPUT"
    exit 1
fi
adb -s "$DEV_ID" forward tcp:8765 tcp:8765
adb -s "$DEV_ID" forward tcp:8766 tcp:8766
adb -s "$DEV_ID" forward tcp:8767 tcp:8767
adb -s "$DEV_ID" forward tcp:8768 tcp:8768

sleep 2
echo "[✓] Privileged gateway health:"
HEALTH=$(curl -fsS http://127.0.0.1:8765/health)
if ! echo "$HEALTH" | grep -q '"gateway":"ready"'; then
    echo "[✗] Gateway health identity check failed: $HEALTH"
    exit 1
fi
echo "$HEALTH"
echo
