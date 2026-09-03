#!/usr/bin/env bash
# Give the privileged gateway one narrowly scoped phh-su capability.
# The GSI includes phh-su but omits its policy UI/database, so app-originated
# commands are otherwise denied even though adb-shell su works.

set -euo pipefail

DEV_ID=${1:?ADB device id is required}
PACKAGE=${2:-com.phoneagent.gateway}
SU_BASE=/data/data/me.phh.superuser
SU_DB=$SU_BASE/databases/su.sqlite
RECOVERY_COMMAND='old=$(pidof audioserver || true); if [ -n "$old" ]; then killall audioserver || exit 1; fi; i=0; while [ "$i" -lt 50 ]; do new=$(pidof audioserver || true); if [ -n "$new" ] && [ "$new" != "$old" ]; then echo "$old->$new"; exit 0; fi; i=$((i + 1)); sleep 0.1; done; exit 2'

if ! adb -s "$DEV_ID" shell su -v 2>/dev/null | grep -q 'me.phh.superuser'; then
    echo "[!] phh-su is not installed; app-side audioserver recovery was not provisioned."
    exit 0
fi

APP_UID=$(
    adb -s "$DEV_ID" shell \
        "su -c 'stat -c %u /data/user/0/$PACKAGE'" 2>/dev/null \
        | tr -d '\r\n'
)
if ! [[ "$APP_UID" =~ ^[0-9]+$ ]]; then
    echo "[✗] Could not resolve the Android uid for $PACKAGE."
    exit 1
fi

SQL="CREATE TABLE IF NOT EXISTS uid_policy (uid INTEGER, policy TEXT, until INTEGER, command TEXT);
DELETE FROM uid_policy WHERE uid=$APP_UID;
INSERT INTO uid_policy (uid, policy, until, command) VALUES ($APP_UID, 'allow', 0, '$RECOVERY_COMMAND');"

adb -s "$DEV_ID" shell "su -c 'mkdir -p $SU_BASE/databases'"
printf '%s\n' "$SQL" | adb -s "$DEV_ID" shell "su -c 'sqlite3 $SU_DB'"
adb -s "$DEV_ID" shell \
    "su -c 'chown -R root:root $SU_BASE && chmod 0700 $SU_BASE $SU_BASE/databases && chmod 0600 $SU_DB'"

POLICY=$(
    adb -s "$DEV_ID" shell \
        "su -c 'sqlite3 $SU_DB \"SELECT policy || char(58) || command FROM uid_policy WHERE uid=$APP_UID;\"'" \
        | tr -d '\r'
)
if [ "$POLICY" != "allow:$RECOVERY_COMMAND" ]; then
    echo "[✗] phh-su audioserver recovery policy verification failed: ${POLICY:-missing}"
    exit 1
fi
echo "[✓] phh-su permits uid $APP_UID to run only: $RECOVERY_COMMAND"
