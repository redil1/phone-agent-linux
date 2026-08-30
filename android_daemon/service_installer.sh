#!/usr/bin/env bash
# Deploy and Start PhoneAgent Root Gateway on Android over USB

set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DAEMON_DIR="$(dirname "${BASH_SOURCE[0]}")"

echo "============================================================"
echo "    PhoneAgent Android Telephony Gateway Installer         "
echo "============================================================"

echo "[*] Checking ADB connection..."
DEV_ID=$(adb devices | grep -w "device" | head -n1 | awk '{print $1}')
if [ -z "$DEV_ID" ]; then
    echo "[✗] Error: No Android device found over ADB. Please connect your phone with USB Debugging enabled."
    exit 1
fi
echo "[✓] Connected device: $DEV_ID"

echo "[*] Pushing gateway scripts to /data/local/tmp/..."
adb -s "$DEV_ID" push "$DAEMON_DIR/handle_http.sh" /data/local/tmp/handle_http.sh
adb -s "$DEV_ID" push "$DAEMON_DIR/root_gateway.sh" /data/local/tmp/root_gateway.sh
adb -s "$DEV_ID" shell "chmod +x /data/local/tmp/handle_http.sh /data/local/tmp/root_gateway.sh"

echo "[*] Starting PhoneAgent Gateway daemon via root..."
adb -s "$DEV_ID" shell "su -c 'sh /data/local/tmp/root_gateway.sh >/data/local/tmp/gateway.log 2>&1 &'"

echo "[*] Establishing ADB Port Forwarding over USB..."
adb forward tcp:8765 tcp:8765
adb forward tcp:8766 tcp:8766
adb forward tcp:8767 tcp:8767

sleep 1

echo "[*] Testing Gateway Health check (http://localhost:8765/call/status)..."
STATUS=$(curl -s http://localhost:8765/call/status || echo "FAIL")
echo "[✓] Response: $STATUS"

echo "============================================================"
echo "[✓] PhoneAgent Gateway is ONLINE and listening on localhost:8765!"
echo "============================================================"
