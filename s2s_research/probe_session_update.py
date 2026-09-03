import asyncio
import fractions
import json
import logging
import uuid

import av
from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription
from curl_cffi.curl import CurlMime
from curl_cffi.requests import AsyncSession

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SessionUpdateProbe")

GIZMO_ID = "g-6a8f59bfdfc8819191e85d3cfa8fd722"

class StableTrack(AudioStreamTrack):
    kind = "audio"
    def __init__(self):
        super().__init__()
        self._pts = 0
    async def recv(self):
        frame = av.AudioFrame(format="s16", layout="mono", samples=960)
        frame.sample_rate = 48000
        frame.time_base = fractions.Fraction(1, 48000)
        frame.pts = self._pts
        self._pts += 960
        for p in frame.planes:
            p.update(b"\x00" * 1920)
        await asyncio.sleep(0.02)
        return frame

async def test_session_update():
    auth = ChatGPTAuthManager()
    token = auth.get_token()

    pc = RTCPeerConnection()
    pc.addTrack(StableTrack())
    dc = pc.createDataChannel("oai-events", id=0, negotiated=True)

    transcript = []

    @dc.on("open")
    def on_open():
        logger.info("DC Open! Sending session.update and system instruction...")
        # 1. Update session instructions
        update_event = {
            "type": "session.update",
            "session": {
                "instructions": (
                    "You are Aziz, Senior Sales Director at OXzoon IPTV. "
                    "You are on a live phone call with a customer. "
                    "Always speak concise, professional French (1-2 sentences max). "
                    "Our pricing: Essential is 10€/mo, Family is 15€/mo, Premium is 20€/mo. "
                    "Never ask 'what is on your mind'. Always stay in character as Aziz from OXzoon."
                ),
                "voice": "coral"
            }
        }
        dc.send(json.dumps(update_event))
        
        # 2. Simulate customer greeting
        user_event = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Allô ? Oui bonjour, c'est qui ?"}]
            }
        }
        dc.send(json.dumps(user_event))
        dc.send(json.dumps({"type": "response.create"}))

    @dc.on("message")
    def on_msg(msg):
        try:
            ev = json.loads(msg)
            if ev.get("type") == "data_message" and isinstance(ev.get("data"), str):
                try:
                    inner = json.loads(ev["data"])
                    if isinstance(inner, dict):
                        ev = inner
                except Exception:
                    pass
            t = ev.get("type") or ev.get("payload", {}).get("type")
            logger.info("DC Event received: %s", t)
            if t == "response.audio_transcript.delta":
                delta = ev.get("delta", "")
                if delta:
                    transcript.append(delta)
                    print(delta, end="", flush=True)
            elif t == "response.audio_transcript.done":
                print("\n")
        except Exception:
            pass

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    session_payload = {
        "backend_reasoning_effort": "instant",
        "conversation_id": None,
        "language_code": "fr",
        "requested_default_model": "",
        "voice": "coral",
        "voice_session_id": str(uuid.uuid4()).upper(),
        "voice_status_request_id": str(uuid.uuid4()).upper(),
        "timezone_offset_min": -60,
        "timezone": "UTC",
        "user_locale": "fr-FR",
        "voice_mode": "wingman",
        "model_slug": "",
        "model_slug_advanced": "",
        "client_tools": [],
        "history_and_training_disabled": False,
        "conversation_mode": {
            "kind": "gizmo_interaction",
            "gizmo_id": GIZMO_ID
        },
        "chat_mode": "chat",
        "backend_model": "auto",
        "enable_message_streaming": True,
    }

    url = "https://chatgpt.com/realtime/wm?dcid=0"
    headers = {
        "Authorization": f"Bearer {token}",
        "OAI-Language": "fr-FR",
        "OAI-Device-Id": str(uuid.uuid4()),
        "OAI-Session-Id": str(uuid.uuid4()),
        "OAI-Client-Version": "prod-307d0cec678653ceadb0418730cb20e04efd95f9",
        "OAI-Client-Build-Number": "9878295",
        "X-OpenAI-Target-Path": "/realtime/wm",
        "X-OpenAI-Target-Route": "/realtime/wm",
    }

    mp = CurlMime()
    mp.addpart(name="sdp", data=pc.localDescription.sdp.encode("utf-8"))
    mp.addpart(name="session", data=json.dumps(session_payload).encode("utf-8"))

    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.post(url, headers=headers, multipart=mp, timeout=15)
        if res.status_code in (200, 201):
            await pc.setRemoteDescription(RTCSessionDescription(sdp=res.text, type="answer"))

    await asyncio.sleep(8.0)
    full = "".join(transcript).strip()
    logger.info("=== FULL MODEL SPOKEN RESPONSE ===\n%s\n", full)
    await pc.close()

if __name__ == "__main__":
    asyncio.run(test_session_update())
