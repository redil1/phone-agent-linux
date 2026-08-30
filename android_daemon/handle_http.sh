#!/system/bin/sh
# PhoneAgent Single HTTP Request Handler (Robust & High-Speed)

read -r REQUEST_LINE
[ -z "$REQUEST_LINE" ] && exit 0

METHOD=$(echo "$REQUEST_LINE" | awk '{print $1}')
FULL_PATH=$(echo "$REQUEST_LINE" | awk '{print $2}')
PATH_URI=$(echo "$FULL_PATH" | awk -F'?' '{print $1}')
QUERY_STR=$(echo "$FULL_PATH" | awk -F'?' '{print $2}')

# Read remaining headers
CONTENT_LENGTH=0
while IFS= read -r HEADER; do
    HEADER_CLEAN=$(echo "$HEADER" | tr -d '\r\n')
    [ -z "$HEADER_CLEAN" ] && break
    case "$HEADER_CLEAN" in
        [Cc][Oo][Nn][Tt][Ee][Nn][Tt]-[Ll][Ee][Nn][Gg][Tt][Hh]:*)
            CONTENT_LENGTH=$(echo "$HEADER_CLEAN" | awk '{print $2}')
            ;;
    esac
done

BODY=""
if [ "$CONTENT_LENGTH" -gt 0 ]; then
    BODY=$(dd bs=1 count="$CONTENT_LENGTH" 2>/dev/null)
fi

RESPONSE_BODY=""
HTTP_STATUS="200 OK"

case "$PATH_URI" in
    "/call/status"|"status")
        DUMP=$(dumpsys telephony.registry | grep -E 'mCallState|mCallIncomingNumber' | head -n 2)
        STATE_CODE=$(echo "$DUMP" | grep 'mCallState' | head -n 1 | awk -F'=' '{print $2}' | tr -d ' \r\n')
        CALLER_NUM=$(echo "$DUMP" | grep 'mCallIncomingNumber' | head -n 1 | awk -F'=' '{print $2}' | tr -d ' \r\n')
        STATE_STR="IDLE"
        if [ "$STATE_CODE" = "1" ]; then
            STATE_STR="RINGING"
        elif [ "$STATE_CODE" = "2" ]; then
            STATE_STR="ACTIVE"
        fi
        RESPONSE_BODY="{\"status\":\"ok\",\"state\":\"$STATE_STR\",\"state_code\":${STATE_CODE:-0},\"incoming_number\":\"$CALLER_NUM\"}"
        ;;
    "/call/dial"|"dial")
        TARGET_NUM=$(echo "$BODY" | grep -o '"number"[[:space:]]*:[[:space:]]*"[^"]*' | cut -d'"' -f4)
        if [ -z "$TARGET_NUM" ]; then
            TARGET_NUM=$(echo "$QUERY_STR" | grep -o 'number=[^&]*' | cut -d'=' -f2)
        fi
        if [ -n "$TARGET_NUM" ]; then
            # Decode URL encoded characters if any
            TARGET_NUM=$(echo "$TARGET_NUM" | sed 's/%2B/+/g; s/%20//g')
            am start -a android.intent.action.CALL -d "tel:$TARGET_NUM" >/dev/null 2>&1 || true
            am start -a android.intent.action.DIAL -d "tel:$TARGET_NUM" >/dev/null 2>&1 || true
            sleep 0.3
            input keyevent KEYCODE_CALL >/dev/null 2>&1 || true
            RESPONSE_BODY="{\"status\":\"ok\",\"action\":\"dialing\",\"number\":\"$TARGET_NUM\"}"
        else
            HTTP_STATUS="400 Bad Request"
            RESPONSE_BODY="{\"status\":\"error\",\"message\":\"Missing number parameter\"}"
        fi
        ;;
    "/call/answer"|"answer")
        input keyevent KEYCODE_HEADSETHOOK >/dev/null 2>&1 || input keyevent KEYCODE_CALL >/dev/null 2>&1
        RESPONSE_BODY="{\"status\":\"ok\",\"action\":\"answered\"}"
        ;;
    "/call/hangup"|"hangup")
        input keyevent KEYCODE_ENDCALL >/dev/null 2>&1
        RESPONSE_BODY="{\"status\":\"ok\",\"action\":\"hung_up\"}"
        ;;
    "/call/dtmf"|"dtmf")
        DIGIT=$(echo "$BODY" | grep -o '"digit"[[:space:]]*:[[:space:]]*"[^"]*' | cut -d'"' -f4)
        if [ -z "$DIGIT" ]; then
            DIGIT=$(echo "$QUERY_STR" | grep -o 'digit=[^&]*' | cut -d'=' -f2)
        fi
        if [ -n "$DIGIT" ]; then
            case "$DIGIT" in
                [0-9]) input keyevent "KEYCODE_$DIGIT" >/dev/null 2>&1 ;;
                "*") input keyevent KEYCODE_STAR >/dev/null 2>&1 ;;
                "#") input keyevent KEYCODE_POUND >/dev/null 2>&1 ;;
            esac
            RESPONSE_BODY="{\"status\":\"ok\",\"action\":\"dtmf_sent\",\"digit\":\"$DIGIT\"}"
        else
            HTTP_STATUS="400 Bad Request"
            RESPONSE_BODY="{\"status\":\"error\",\"message\":\"Missing digit parameter\"}"
        fi
        ;;
    *)
        HTTP_STATUS="404 Not Found"
        RESPONSE_BODY="{\"status\":\"error\",\"message\":\"Endpoint not found: $PATH_URI\"}"
        ;;
esac

RESP_LEN=${#RESPONSE_BODY}
printf "HTTP/1.1 %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\nAccess-Control-Allow-Origin: *\r\n\r\n%s" "$HTTP_STATUS" "$RESP_LEN" "$RESPONSE_BODY"
