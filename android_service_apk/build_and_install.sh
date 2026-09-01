#!/usr/bin/env bash
# Build, Package, Sign, and Install PhoneAgent Native Headless Service APK

set -e

BUILD_ONLY=false
if [ "${1:-}" = "--build-only" ]; then
    BUILD_ONLY=true
elif [ -n "${1:-}" ]; then
    echo "Usage: $0 [--build-only]"
    exit 2
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}}"
if [ ! -d "$SDK" ] && [ -d "/Users/aziz/Library/Android/sdk" ]; then
    SDK="/Users/aziz/Library/Android/sdk"
fi
BT_DIR="$(ls -d "$SDK/build-tools/"* 2>/dev/null | sort -V | tail -n1 || echo "$SDK/build-tools/34.0.0")"
PLAT_DIR="$(ls -d "$SDK/platforms/android-"* 2>/dev/null | sort -V | tail -n1 || echo "$SDK/platforms/android-34")"

AAPT2="$BT_DIR/aapt2"
D8="$BT_DIR/d8"
ZIPALIGN="$BT_DIR/zipalign"
APKSIGNER="$BT_DIR/apksigner"
ANDROID_JAR="$PLAT_DIR/android.jar"

BUILD_DIR="$DIR/build"
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

# 2. Compile Java Source Files with javac
echo "[*] Compiling Java source files with javac..."
# The QR decoder is vendored rather than fetched at build time so the APK is
# reproducible offline. It is pure Java with no Android dependencies.
LIBS="$(find "$DIR/libs" -name '*.jar' 2>/dev/null | tr '\n' ':')"
javac -encoding UTF-8 \
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
cd "$BUILD_DIR"
jar -uf "$BUILD_DIR/unaligned.apk" classes.dex
cd "$DIR"

# 5. Zipalign APK
echo "[*] Aligning APK with zipalign..."
"$ZIPALIGN" -f -v 4 "$BUILD_DIR/unaligned.apk" "$BUILD_DIR/aligned.apk" >/dev/null

# 6. Generate Keystore and Sign APK
KEYSTORE="$DIR/debug.keystore"
if [ ! -f "$KEYSTORE" ]; then
    echo "[*] Generating debug signing keystore..."
    keytool -genkey -v -keystore "$KEYSTORE" -alias debug -keyalg RSA -keysize 2048 -validity 10000 \
        -storepass android -keypass android -dname "CN=PhoneAgent, OU=AI, O=Agent, L=Local, S=State, C=US"
fi

echo "[*] Signing APK with apksigner..."
FINAL_APK="$DIR/PhoneAgentGateway.apk"
"$APKSIGNER" sign \
    --ks "$KEYSTORE" \
    --ks-pass pass:android \
    --key-pass pass:android \
    --ks-key-alias debug \
    --out "$FINAL_APK" \
    "$BUILD_DIR/aligned.apk"

echo "[✓] APK Built & Signed: $FINAL_APK"

if [ "$BUILD_ONLY" = true ]; then
    echo "[✓] Build-only requested; device installation skipped."
    exit 0
fi

# 7. Install to Connected Android Device
DEV_ID=$(adb devices | grep -w "device" | head -n1 | awk '{print $1}')
if [ -n "$DEV_ID" ]; then
    echo "[*] Installing APK to device ($DEV_ID)..."
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
    adb -s "$DEV_ID" shell "pm grant com.phoneagent.gateway android.permission.CALL_PHONE" 2>/dev/null || true
    adb -s "$DEV_ID" shell "pm grant com.phoneagent.gateway android.permission.READ_PHONE_STATE" 2>/dev/null || true
    adb -s "$DEV_ID" shell "pm grant com.phoneagent.gateway android.permission.RECORD_AUDIO" 2>/dev/null || true
    adb -s "$DEV_ID" shell "pm grant com.phoneagent.gateway android.permission.READ_CALL_LOG" 2>/dev/null || true
    adb -s "$DEV_ID" shell "pm grant com.phoneagent.gateway android.permission.POST_NOTIFICATIONS" 2>/dev/null || true

    echo "[*] Assigning PhoneAgent as the Android default dialer..."
    adb -s "$DEV_ID" shell "cmd role add-role-holder android.app.role.DIALER com.phoneagent.gateway 0"
    ROLE_HOLDERS=$(adb -s "$DEV_ID" shell "cmd role get-role-holders android.app.role.DIALER 0" | tr -d '\r')
    if ! echo "$ROLE_HOLDERS" | grep -q '^com.phoneagent.gateway$'; then
        echo "[✗] ROLE_DIALER assignment failed. Open the PhoneAgent app and grant the role manually."
        exit 1
    fi

    echo "[*] Stopping the legacy shell listener to free control port 8765..."
    adb -s "$DEV_ID" shell "su -c 'pkill -f \"[r]oot_gateway.sh\" 2>/dev/null || true; pkill -f \"[n]etcat -L -p 8765\" 2>/dev/null || true'" || true

    echo "[*] Starting Headless Gateway Service..."
    adb -s "$DEV_ID" shell "am start-foreground-service -n com.phoneagent.gateway/.GatewayService"

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
    echo "[✓] Health Status: $STATUS"

    echo "============================================================"
    echo "  NATIVE PHONEAGENT CONTROL & AUDIO SERVERS ARE ONLINE     "
    echo "============================================================"
else
    echo "[!] No ADB device found to install APK."
fi
