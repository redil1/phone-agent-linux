#!/system/bin/sh
# PhoneAgent Android Root Gateway Server

PORT_HTTP=8765

echo "[*] Initializing PhoneAgent Root Gateway on Android..."

# Kill previous instances
pkill -f "handle_http.sh" 2>/dev/null
pkill -f "nc.*$PORT_HTTP" 2>/dev/null

chmod +x /data/local/tmp/handle_http.sh

echo "[✓] Starting HTTP Daemon on port $PORT_HTTP..."
toybox netcat -L -p "$PORT_HTTP" -s 127.0.0.1 /system/bin/sh /data/local/tmp/handle_http.sh
