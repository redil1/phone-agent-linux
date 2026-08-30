#!/usr/bin/env bash

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANDROID_JAR="/Users/aziz/Library/Android/sdk/platforms/android-34/android.jar"
TEST_CLASSES="$DIR/build/test-classes"

mkdir -p "$TEST_CLASSES"
javac -encoding UTF-8 \
    -cp "$ANDROID_JAR:$DIR/build/classes" \
    -d "$TEST_CLASSES" \
    "$DIR/testsrc/com/phoneagent/gateway/ProtocolCodecInteropTest.java"

java -cp "$ANDROID_JAR:$DIR/build/classes:$TEST_CLASSES" \
    com.phoneagent.gateway.ProtocolCodecInteropTest
