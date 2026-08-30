#!/usr/bin/env bash
# Build a persistent PhoneAgent GSI by adding the privileged APK and allowlist
# to a copy of the pristine ext4 system image. The base image is never edited.

set -euo pipefail

usage() {
    echo "Usage: $0 --base-image PATH --output PATH"
    exit 2
}

BASE_IMAGE=""
OUTPUT_IMAGE=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --base-image) BASE_IMAGE="${2:-}"; shift 2 ;;
        --output) OUTPUT_IMAGE="${2:-}"; shift 2 ;;
        *) usage ;;
    esac
done
[ -n "$BASE_IMAGE" ] && [ -n "$OUTPUT_IMAGE" ] || usage
[ -f "$BASE_IMAGE" ] || { echo "[x] Base image not found: $BASE_IMAGE"; exit 1; }
[ ! -e "$OUTPUT_IMAGE" ] || { echo "[x] Refusing to overwrite: $OUTPUT_IMAGE"; exit 1; }

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APK="$DIR/PhoneAgentGateway.apk"
ALLOWLIST="$DIR/privapp-permissions-com.phoneagent.gateway.xml"
[ -f "$APK" ] && [ -f "$ALLOWLIST" ] || {
    echo "[x] Build the APK first; APK or allowlist is missing."
    exit 1
}

find_e2fs_tool() {
    local name="$1"
    if command -v "$name" >/dev/null 2>&1; then
        command -v "$name"
    elif [ -x "/opt/homebrew/opt/e2fsprogs/sbin/$name" ]; then
        echo "/opt/homebrew/opt/e2fsprogs/sbin/$name"
    else
        echo "[x] Missing $name. Install Homebrew e2fsprogs first." >&2
        exit 1
    fi
}

DEBUGFS="$(find_e2fs_tool debugfs)"
E2FSCK="$(find_e2fs_tool e2fsck)"
mkdir -p "$(dirname "$OUTPUT_IMAGE")"

BASE_SIZE="$(stat -f '%z' "$BASE_IMAGE")"
echo "[*] Cloning pristine system image ($BASE_SIZE bytes)..."
cp -c "$BASE_IMAGE" "$OUTPUT_IMAGE"
[ "$(stat -f '%z' "$OUTPUT_IMAGE")" = "$BASE_SIZE" ] || {
    echo "[x] Output image size changed during clone."
    exit 1
}

TMP_DIR="$(mktemp -d /tmp/phoneagent-persistent-gsi.XXXXXX)"
SELINUX_VALUE="$TMP_DIR/system-file.selinux"

# Reuse the exact null-terminated SELinux value from the base image instead of
# reconstructing it as a shell string.
"$DEBUGFS" -R "ea_get -f $SELINUX_VALUE /system/priv-app security.selinux" \
    "$BASE_IMAGE" >/dev/null 2>&1
[ "$(stat -f '%z' "$SELINUX_VALUE")" = "26" ] || {
    echo "[x] Unexpected SELinux xattr length in base image."
    exit 1
}

echo "[*] Adding privileged PhoneAgent payload..."
"$DEBUGFS" -w -R 'mkdir /system/priv-app/PhoneAgentGateway' "$OUTPUT_IMAGE"
"$DEBUGFS" -w -R \
    "write $APK /system/priv-app/PhoneAgentGateway/PhoneAgentGateway.apk" \
    "$OUTPUT_IMAGE"
"$DEBUGFS" -w -R \
    "write $ALLOWLIST /system/etc/permissions/privapp-permissions-com.phoneagent.gateway.xml" \
    "$OUTPUT_IMAGE"

DIR_TARGET="/system/priv-app/PhoneAgentGateway"
APK_TARGET="$DIR_TARGET/PhoneAgentGateway.apk"
ALLOWLIST_TARGET="/system/etc/permissions/privapp-permissions-com.phoneagent.gateway.xml"
for target in "$DIR_TARGET" "$APK_TARGET" "$ALLOWLIST_TARGET"; do
    "$DEBUGFS" -w -R "set_inode_field $target uid 0" "$OUTPUT_IMAGE" >/dev/null
    "$DEBUGFS" -w -R "set_inode_field $target gid 0" "$OUTPUT_IMAGE" >/dev/null
    "$DEBUGFS" -w -R "ea_set -f $SELINUX_VALUE $target security.selinux" \
        "$OUTPUT_IMAGE" >/dev/null
done
"$DEBUGFS" -w -R "set_inode_field $DIR_TARGET mode 040755" "$OUTPUT_IMAGE" >/dev/null
"$DEBUGFS" -w -R "set_inode_field $APK_TARGET mode 0100644" "$OUTPUT_IMAGE" >/dev/null
"$DEBUGFS" -w -R "set_inode_field $ALLOWLIST_TARGET mode 0100644" "$OUTPUT_IMAGE" >/dev/null

echo "[*] Verifying embedded bytes and filesystem metadata..."
"$DEBUGFS" -R "dump $APK_TARGET $TMP_DIR/PhoneAgentGateway.apk" \
    "$OUTPUT_IMAGE" >/dev/null 2>&1
"$DEBUGFS" -R "dump $ALLOWLIST_TARGET $TMP_DIR/permissions.xml" \
    "$OUTPUT_IMAGE" >/dev/null 2>&1
[ "$(shasum -a 256 "$APK" | awk '{print $1}')" = \
  "$(shasum -a 256 "$TMP_DIR/PhoneAgentGateway.apk" | awk '{print $1}')" ] || {
    echo "[x] Embedded APK hash mismatch."
    exit 1
}
[ "$(shasum -a 256 "$ALLOWLIST" | awk '{print $1}')" = \
  "$(shasum -a 256 "$TMP_DIR/permissions.xml" | awk '{print $1}')" ] || {
    echo "[x] Embedded allowlist hash mismatch."
    exit 1
}
for target in "$DIR_TARGET" "$APK_TARGET" "$ALLOWLIST_TARGET"; do
    STAT_OUTPUT="$("$DEBUGFS" -R "stat $target" "$OUTPUT_IMAGE" 2>/dev/null)"
    echo "$STAT_OUTPUT" | grep -q 'User:     0   Group:     0' || {
        echo "[x] Wrong ownership for $target"
        exit 1
    }
    echo "$STAT_OUTPUT" | grep -q 'security.selinux (26).*u:object_r:system_file:s0' || {
        echo "[x] Wrong SELinux metadata for $target"
        exit 1
    }
done
"$DEBUGFS" -R "stat $DIR_TARGET" "$OUTPUT_IMAGE" 2>/dev/null | grep -q 'Mode:  0755'
"$DEBUGFS" -R "stat $APK_TARGET" "$OUTPUT_IMAGE" 2>/dev/null | grep -q 'Mode:  0644'
"$DEBUGFS" -R "stat $ALLOWLIST_TARGET" "$OUTPUT_IMAGE" 2>/dev/null | grep -q 'Mode:  0644'
"$E2FSCK" -fn "$OUTPUT_IMAGE"

shasum -a 256 "$OUTPUT_IMAGE" | tee "$OUTPUT_IMAGE.sha256"
echo "[+] Persistent GSI created without modifying the base image:"
echo "    $OUTPUT_IMAGE"
echo "[i] Temporary verification files: $TMP_DIR"

