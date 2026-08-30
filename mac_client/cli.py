#!/usr/bin/env python3
"""PhoneAgent Interactive Terminal Controller & Monitor.

Provides a rich command-line interface to place calls, answer incoming calls,
monitor live telephony state, and test audio streaming over USB.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from phone_agent_gateway.mac_client.gateway_client import CallState, CallStatus, PhoneAgentClient


def format_status(status: CallStatus) -> str:
    color_map = {
        CallState.IDLE: "\033[92m[ IDLE ]\033[0m",
        CallState.RINGING: "\033[93m[ 🔔 RINGING ]\033[0m",
        CallState.DIALING: "\033[94m[ DIALING ]\033[0m",
        CallState.CONNECTING: "\033[94m[ CONNECTING ]\033[0m",
        CallState.ACTIVE: "\033[96m[ 📞 ACTIVE CALL ]\033[0m",
        CallState.HOLDING: "\033[95m[ HOLDING ]\033[0m",
        CallState.DISCONNECTED: "\033[90m[ DISCONNECTED ]\033[0m",
        CallState.UNKNOWN: "\033[91m[ UNKNOWN ]\033[0m",
    }
    badge = color_map.get(status.state, "[ ? ]")
    caller = f" (Caller: {status.incoming_number})" if status.incoming_number else ""
    return f"{badge}{caller}"


def print_banner() -> None:
    print("\033[1;34m" + "=" * 62)
    print("     PHONEAGENT CELLULAR TELEPHONY & AUDIO CONTROLLER     ")
    print("=" * 62 + "\033[0m")
    print("Commands:")
    print("  \033[1mdial <number>\033[0m   - Place outbound phone call (e.g. dial +123456789)")
    print("  \033[1manswer\033[0m          - Answer incoming ringing call")
    print("  \033[1mreject\033[0m          - Reject incoming ringing call")
    print("  \033[1mhangup\033[0m          - End / disconnect active call")
    print("  \033[1mdtmf <0-9|*|#>\033[0m  - Send keypad DTMF digit")
    print("  \033[1mstatus\033[0m          - Check current telephony state")
    print("  \033[1mquit / exit\033[0m     - Exit CLI")
    print("-" * 62)


def main() -> int:
    print_banner()

    try:
        client = PhoneAgentClient()
    except Exception as exc:
        print(f"\033[91m[✗] Connection error: {exc}\033[0m")
        return 1

    def on_state_change(status: CallStatus) -> None:
        print(f"\n\033[1;33m[*] Telephony State Changed:\033[0m {format_status(status)}")
        if status.state == CallState.RINGING:
            print(
                "\033[1;32m[!] INCOMING CALL from "
                f"{status.incoming_number}! Type 'answer' to accept.\033[0m"
            )
        print("\033[1;34mPhoneAgent>\033[0m ", end="", flush=True)

    client.add_call_listener(on_state_change)

    # Initial status check
    try:
        initial = client.get_status()
        print(f"[*] Initial Phone State: {format_status(initial)}\n")
    except Exception as exc:
        print(f"\033[93m[!] Warning: Could not fetch initial state ({exc}).\033[0m\n")

    while True:
        try:
            line = input("\033[1;34mPhoneAgent>\033[0m ").strip()
            if not line:
                continue

            parts = line.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd in ("exit", "quit", "q"):
                print("[*] Exiting PhoneAgent CLI...")
                break

            elif cmd == "status":
                status = client.get_status()
                print(f"[*] Current Status: {format_status(status)}")

            elif cmd == "dial":
                if not arg:
                    print("\033[91m[!] Please provide a phone number: dial <number>\033[0m")
                    continue
                res = client.dial(arg)
                print(f"\033[92m[✓] Dialing {arg}...\033[0m (Response: {res.get('action')})")

            elif cmd == "answer":
                res = client.answer()
                print(f"\033[92m[✓] Answering call...\033[0m (Response: {res.get('action')})")

            elif cmd == "reject":
                res = client.reject()
                print(f"[*] Reject result: {res}")

            elif cmd == "hangup":
                res = client.hangup()
                print(f"\033[93m[✓] Call hung up.\033[0m (Response: {res.get('action')})")

            elif cmd == "dtmf":
                if not arg:
                    print("\033[91m[!] Usage: dtmf <0-9|*|#>\033[0m")
                    continue
                res = client.send_dtmf(arg[0])
                print(f"\033[92m[✓] DTMF digit sent: {arg[0]}\033[0m")

            else:
                print(
                    f"\033[91m[!] Unknown command: {cmd}. Type 'status', "
                    "'dial <number>', 'answer', 'hangup', 'dtmf <digit>', "
                    "or 'exit'.\033[0m"
                )

        except KeyboardInterrupt:
            print("\n[*] Exiting PhoneAgent CLI...")
            break
        except Exception as exc:
            print(f"\033[91m[✗] Error: {exc}\033[0m")

    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
