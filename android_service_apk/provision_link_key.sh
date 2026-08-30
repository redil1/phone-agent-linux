#!/usr/bin/env bash
# Provision one shared PHAG v1 link key into Mac and Android private storage.

set -euo pipefail

if [ "${1:-}" != "--commit" ]; then
    echo "Usage: $0 --commit"
    echo "Creates/reuses a private Mac key and provisions it to the connected gateway app."
    exit 2
fi

DEV_ID=$(adb devices | awk '$2 == "device" {print $1; exit}')
if [ -z "$DEV_ID" ]; then
    echo "[✗] No ADB device is connected."
    exit 1
fi

if ! adb -s "$DEV_ID" shell pm path com.phoneagent.gateway >/dev/null 2>&1; then
    echo "[✗] com.phoneagent.gateway is not installed."
    exit 1
fi

KEY_PATH="${PHONE_AGENT_LINK_KEY_FILE:-$HOME/.config/phone-agent/link.key}"
KEY_DIR=$(dirname "$KEY_PATH")
mkdir -p "$KEY_DIR"
chmod 700 "$KEY_DIR"

if [ -f "$KEY_PATH" ]; then
    KEY_BYTES=$(wc -c < "$KEY_PATH" | tr -d ' ')
    if [ "$KEY_BYTES" -lt 32 ] || [ "$KEY_BYTES" -gt 4096 ]; then
        echo "[✗] Existing key has an invalid length: $KEY_BYTES bytes"
        exit 1
    fi
    echo "[*] Reusing existing Mac link key."
else
    TEMP_KEY=$(mktemp)
    trap 'rm -f "$TEMP_KEY"' EXIT
    openssl rand 32 > "$TEMP_KEY"
    cp "$TEMP_KEY" "$KEY_PATH"
    chmod 600 "$KEY_PATH"
    echo "[*] Generated a new 32-byte Mac link key."
fi

REMOTE_TEMP="/data/local/tmp/phoneagent-link-key"
adb -s "$DEV_ID" push "$KEY_PATH" "$REMOTE_TEMP" >/dev/null
APP_UID=$(adb -s "$DEV_ID" shell dumpsys package com.phoneagent.gateway \
    | awk -F= '/userId=|appId=/{gsub(/\r/, "", $2); gsub(/ /, "", $2); print $2; exit}')
if [ -z "$APP_UID" ]; then
    echo "[✗] Could not resolve the gateway app UID."
    exit 1
fi

adb -s "$DEV_ID" shell "su -c '
    mkdir -p /data/user/0/com.phoneagent.gateway/files &&
    cp $REMOTE_TEMP /data/user/0/com.phoneagent.gateway/files/link.key &&
    chown $APP_UID:$APP_UID /data/user/0/com.phoneagent.gateway/files/link.key &&
    chmod 0600 /data/user/0/com.phoneagent.gateway/files/link.key &&
    restorecon -R /data/user/0/com.phoneagent.gateway/files &&
    rm -f $REMOTE_TEMP
'"

LOCAL_HASH=$(shasum -a 256 "$KEY_PATH" | awk '{print $1}')
PHONE_HASH=$(adb -s "$DEV_ID" shell \
    "su -c 'sha256sum /data/user/0/com.phoneagent.gateway/files/link.key'" \
    | awk '{gsub(/\r/, "", $1); print $1}')
if [ "$LOCAL_HASH" != "$PHONE_HASH" ]; then
    echo "[✗] Mac and Android key hashes do not match."
    exit 1
fi

echo "[✓] Authenticated link key provisioned without printing secret material."
echo "    export PHONE_AGENT_LINK_KEY_FILE=\"$KEY_PATH\""
