#!/usr/bin/env bash

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
ANDROID_JAR="${PHONE_AGENT_ANDROID_JAR:-${SDK:+$SDK/platforms/android-34/android.jar}}"
TEST_CLASSES="$DIR/build/test-classes"

if [[ -z "$ANDROID_JAR" || ! -f "$ANDROID_JAR" ]]; then
    echo "Android 34 android.jar not found; set ANDROID_HOME or PHONE_AGENT_ANDROID_JAR." >&2
    exit 2
fi

mkdir -p "$TEST_CLASSES"
javac --release 17 -encoding UTF-8 \
    -cp "$ANDROID_JAR:$DIR/build/classes" \
    -d "$TEST_CLASSES" \
    "$DIR/testsrc/com/phoneagent/gateway/ProtocolCodecInteropTest.java"

java -cp "$ANDROID_JAR:$DIR/build/classes:$TEST_CLASSES" \
    com.phoneagent.gateway.ProtocolCodecInteropTest
