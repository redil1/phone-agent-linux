#!/usr/bin/env python3
"""
Native WhatsApp Caller & Messaging Python Wrapper
Interfaces with the standalone native binary (built with whatsmeow + meowcaller).
No browser needed.
"""

import os
import sys
import subprocess
import argparse
from typing import Optional, Dict, Any

BINARY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whatsapp-native-caller")

def check_status() -> Dict[str, Any]:
    """Check if the native client is authenticated and paired."""
    if not os.path.exists(BINARY_PATH):
        return {"status": "error", "message": f"Binary not found at {BINARY_PATH}"}
        
    res = subprocess.run([BINARY_PATH, "status"], capture_output=True, text=True)
    out = res.stdout.strip()
    is_logged_in = "status: logged_in" in out
    
    info = {"logged_in": is_logged_in, "raw": out}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    return info

def pair_phone(phone_number: str, country_code: str = "212"):
    """Pair via 8-character pairing code (no camera required)."""
    if not os.path.exists(BINARY_PATH):
        print(f"[-] Error: Binary not found at {BINARY_PATH}")
        sys.exit(1)
    subprocess.run([BINARY_PATH, "pair-phone", phone_number, "--country-code", country_code])

def login():
    """Launch terminal QR pairing for linked device."""
    if not os.path.exists(BINARY_PATH):
        print(f"[-] Error: Binary not found at {BINARY_PATH}")
        sys.exit(1)
    subprocess.run([BINARY_PATH, "login"])

def logout():
    """Clear session database."""
    if not os.path.exists(BINARY_PATH):
        print(f"[-] Error: Binary not found at {BINARY_PATH}")
        sys.exit(1)
    subprocess.run([BINARY_PATH, "logout"])

def send_whatsapp_message(
    phone_number: str,
    message: str,
    country_code: str = "212"
) -> Dict[str, Any]:
    """
    Send a WhatsApp text message to any phone number in the world.
    
    Args:
        phone_number: Target phone number (e.g. "0622586634" or "+212622586634")
        message: The text message content to send
        country_code: Default country code (default: "212")
        
    Returns:
        Dict with execution results
    """
    if not os.path.exists(BINARY_PATH):
        return {"success": False, "error": f"Binary not found at {BINARY_PATH}"}

    cmd = [
        BINARY_PATH, "send-message", phone_number, message,
        "--country-code", country_code
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    out = res.stdout.strip()
    success = res.returncode == 0 and "Message sent successfully" in out

    return {
        "success": success,
        "output": out,
        "error": res.stderr.strip() if not success else None
    }

def call_whatsapp_native(
    phone_number: str,
    video: bool = False,
    country_code: str = "212",
    play_audio: Optional[str] = None,
    record_audio: Optional[str] = None,
    duration: int = 25
) -> Dict[str, Any]:
    """
    Place a native WhatsApp call directly over WhatsApp Multi-Device protocol.
    """
    if not os.path.exists(BINARY_PATH):
        return {"success": False, "error": f"Binary not found at {BINARY_PATH}"}

    # Flags must come before the number: Go's flag package stops parsing at the
    # first positional argument, so anything after it was silently ignored.
    cmd = [
        BINARY_PATH, "call",
        "--country-code", country_code,
        "--duration", str(duration),
    ]

    if video:
        cmd.append("--video")
    if play_audio:
        cmd.extend(["--play", play_audio])
    if record_audio:
        cmd.extend(["--record", record_audio])
    cmd.append(phone_number)

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    logs = []
    call_placed = False
    peer_accepted = False

    for line in process.stdout:
        print(line, end="")
        logs.append(line)
        if "Call placed" in line:
            call_placed = True
        if "ACCEPTED" in line:
            peer_accepted = True

    process.wait()

    return {
        "success": process.returncode == 0 or call_placed,
        "call_placed": call_placed,
        "peer_accepted": peer_accepted,
        "exit_code": process.returncode,
        "logs": "".join(logs)
    }

def main():
    parser = argparse.ArgumentParser(description="WhatsApp Native Standalone Caller & Messenger (No Browser)")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Pair phone command (8-digit code)
    pair_parser = subparsers.add_parser("pair-phone", help="Pair using 8-character code (No camera needed)")
    pair_parser.add_argument("phone", help="Your WhatsApp phone number (e.g. 0660193275)")
    pair_parser.add_argument("--country-code", default="212", help="Default country code (default: 212)")

    # Login command (QR)
    subparsers.add_parser("login", help="Pair device via terminal QR code")
    
    # Status command
    subparsers.add_parser("status", help="Check login status")
    
    # Logout command
    subparsers.add_parser("logout", help="Clear session database")

    # Send Message command
    send_parser = subparsers.add_parser("send-message", help="Send a WhatsApp text message to anyone")
    send_parser.add_argument("number", help="Target phone number (e.g. 0622586634)")
    send_parser.add_argument("message", help="Text message content to send")
    send_parser.add_argument("--country-code", default="212", help="Default country code (default: 212)")

    # Call command
    call_parser = subparsers.add_parser("call", help="Initiate native WhatsApp call")
    call_parser.add_argument("number", help="Phone number to call (e.g. 0622586634)")
    call_parser.add_argument("--video", action="store_true", help="Make a video call")
    call_parser.add_argument("--country-code", default="212", help="Default country code (default: 212)")
    call_parser.add_argument("--play", help="Path to .mp3 or .wav audio file to play during call")
    call_parser.add_argument("--record", help="Path to .wav file to record call audio")
    call_parser.add_argument("--duration", type=int, default=25, help="Duration in seconds before hangup")

    args = parser.parse_args()

    if args.command == "pair-phone":
        pair_phone(args.phone, country_code=args.country_code)
    elif args.command == "login":
        login()
    elif args.command == "status":
        st = check_status()
        print("Status:", st)
    elif args.command == "logout":
        logout()
    elif args.command == "send-message":
        res = send_whatsapp_message(args.number, args.message, country_code=args.country_code)
        if res["success"]:
            print(res["output"])
        else:
            print("[-] Failed:", res.get("error") or res.get("output"))
            sys.exit(1)
    elif args.command == "call":
        res = call_whatsapp_native(
            phone_number=args.number,
            video=args.video,
            country_code=args.country_code,
            play_audio=args.play,
            record_audio=args.record,
            duration=args.duration
        )
        if not res["success"]:
            sys.exit(1)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
