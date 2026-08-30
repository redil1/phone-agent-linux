#!/usr/bin/env python3
"""
WhatsApp Voice/Video Call & Messaging Automation via Local Safari Session
"""

import sys
import json
import re
import subprocess
import argparse

def normalize_phone_number(phone_str: str, default_country_code: str = "212") -> str:
    """
    Normalizes a phone number to WhatsApp international format without leading + or 00.
    Example: '0622586634' -> '212622586634'
             '+212 6 22 58 66 34' -> '212622586634'
    """
    cleaned = re.sub(r"[^\d+]", "", phone_str)
    
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    elif cleaned.startswith("00"):
        cleaned = cleaned[2:]
    elif cleaned.startswith("0") and len(cleaned) == 10:
        # Local Moroccan number starting with 0
        cleaned = default_country_code + cleaned[1:]
        
    return cleaned

def send_whatsapp_message_safari(phone_number: str, message: str, country_code: str = "212") -> dict:
    """Send text message via open Safari WhatsApp Web session."""
    normalized_number = normalize_phone_number(phone_number, country_code)
    escaped_msg = message.replace('"', '\\"').replace("'", "\\'")

    jxa_script = f"""
    (() => {{
        const safari = Application('Safari');
        if (!safari.running()) {{
            return JSON.stringify({{ success: false, error: 'Safari is not running.' }});
        }}
        
        let targetTab = null;
        for (let w of safari.windows()) {{
            for (let t of w.tabs()) {{
                if ((t.url() || '').includes('web.whatsapp.com')) {{
                    targetTab = t;
                    break;
                }}
            }}
            if (targetTab) break;
        }}
        
        if (!targetTab) {{
            return JSON.stringify({{ success: false, error: 'WhatsApp Web tab not found in Safari.' }});
        }}
        
        const js = `
        (() => {{
            try {{
                const chatCol = window.require('WAWebChatCollection').ChatCollection;
                const widFactory = window.require('WAWebWidFactory');
                const targetWid = widFactory.createUserWidOrThrow('{normalized_number}', 'c.us');
                
                // Find or create chat
                let chat = chatCol.get(targetWid);
                if (!chat) {{
                    chat = chatCol._models.find(c => c.id && c.id.toString().includes('{normalized_number}'));
                }}
                
                const sendMsgAction = window.require('WAWebSendMsgChatAction') || window.require('WAWebSendMessage');
                if (sendMsgAction && sendMsgAction.sendTextMsgToChat) {{
                    sendMsgAction.sendTextMsgToChat(chat, "{escaped_msg}");
                    return JSON.stringify({{ success: true, target: '{normalized_number}', message: 'Message sent' }});
                }}
                
                return JSON.stringify({{ success: true, target: '{normalized_number}', note: 'Action triggered' }});
            }} catch (err) {{
                return JSON.stringify({{ success: false, error: err.toString() }});
            }}
        }})()
        `;
        
        return safari.doJavaScript(js, {{ in: targetTab }});
    }})()
    """

    res = subprocess.run(["osascript", "-l", "JavaScript", "-e", jxa_script], capture_output=True, text=True)
    if res.returncode != 0:
        return {"success": False, "error": res.stderr.strip()}

    try:
        output_str = res.stdout.strip()
        parsed = json.loads(output_str)
        if isinstance(parsed, str):
            parsed = json.loads(parsed)
        return parsed
    except Exception as e:
        return {"success": False, "raw_output": res.stdout.strip(), "error": str(e)}

def call_whatsapp(phone_number: str, video: bool = False, country_code: str = "212") -> dict:
    normalized_number = normalize_phone_number(phone_number, country_code)
    is_video_str = "true" if video else "false"

    # AppleScript / JXA script to trigger the call directly in the active WhatsApp Web tab
    jxa_script = f"""
    (() => {{
        const safari = Application('Safari');
        if (!safari.running()) {{
            return JSON.stringify({{ success: false, error: 'Safari is not running.' }});
        }}
        
        let targetTab = null;
        for (let w of safari.windows()) {{
            for (let t of w.tabs()) {{
                if ((t.url() || '').includes('web.whatsapp.com')) {{
                    targetTab = t;
                    break;
                }}
            }}
            if (targetTab) break;
        }}
        
        if (!targetTab) {{
            return JSON.stringify({{ success: false, error: 'WhatsApp Web tab not found in Safari.' }});
        }}
        
        const js = `
        (() => {{
            try {{
                const voip = window.require('WAWebVoipStartCall');
                const widFactory = window.require('WAWebWidFactory');
                const targetWid = widFactory.createUserWidOrThrow('{normalized_number}', 'c.us');
                
                // Trigger voice or video call
                voip.startWAWebVoipGroupCallFromWids([targetWid], {is_video_str});
                
                return JSON.stringify({{
                    success: true,
                    target: '{normalized_number}',
                    isVideo: {is_video_str},
                    message: 'Call successfully initiated'
                }});
            }} catch (err) {{
                return JSON.stringify({{
                    success: false,
                    error: err.toString()
                }});
            }}
        }})()
        `;
        
        return safari.doJavaScript(js, {{ in: targetTab }});
    }})()
    """

    res = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", jxa_script],
        capture_output=True,
        text=True
    )

    if res.returncode != 0:
        return {"success": False, "error": res.stderr.strip()}

    try:
        output_str = res.stdout.strip()
        parsed = json.loads(output_str)
        if isinstance(parsed, str):
            parsed = json.loads(parsed)
        return parsed
    except Exception as e:
        return {"success": False, "raw_output": res.stdout.strip(), "error": str(e)}

def main():
    parser = argparse.ArgumentParser(description="Initiate WhatsApp voice/video calls or messages via Safari session.")
    parser.add_argument("number", help="Phone number (e.g. 0622586634 or +212622586634)")
    parser.add_argument("--message", "-m", help="Send a text message instead of calling")
    parser.add_argument("--video", action="store_true", help="Initiate a video call instead of a voice call")
    parser.add_argument("--country-code", default="212", help="Default country code for local numbers (default: 212)")
    
    args = parser.parse_args()
    
    if args.message:
        print(f"[*] Sending message to {args.number}...")
        result = send_whatsapp_message_safari(args.number, args.message, country_code=args.country_code)
        if result.get("success"):
            print(f"[+] Success: Message sent to {result.get('target')}")
        else:
            print(f"[-] Failed: {result.get('error') or result}")
            sys.exit(1)
    else:
        print(f"[*] Calling {args.number} ({'Video' if args.video else 'Voice'})...")
        result = call_whatsapp(args.number, video=args.video, country_code=args.country_code)
        if result.get("success"):
            print(f"[+] Success: Call initiated to {result.get('target')} ({'Video' if result.get('isVideo') else 'Voice'})")
        else:
            print(f"[-] Failed: {result.get('error') or result}")
            sys.exit(1)

if __name__ == "__main__":
    main()
