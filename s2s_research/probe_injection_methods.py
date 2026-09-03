"""S2S Instruction Injection Experimentation Runner.

Tests multiple injection mechanisms against the live ChatGPT Realtime WebRTC Gateway:
1. Signaling Session Payload variations
2. DataChannel session.update & system items
3. Prompt engineering & roleplay wrappers
4. Conversation pre-creation binding
"""

import asyncio
import json
import logging
import uuid

import av
from aiortc import AudioStreamTrack, RTCPeerConnection
from curl_cffi.curl import CurlMime
from curl_cffi.requests import AsyncSession

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("S2SExperiment")

PERSONA_PROMPT = (
    "You are Aziz, Sales Director at OXzoon, a premium IPTV provider. "
    "You speak fluent French and English. "
    "Never say 'what's on your mind'. Greet the customer with: "
    "'Bonjour, ici Aziz de chez OXzoon. Comment puis-je vous aider ?'"
)

class DummyAudioTrack(AudioStreamTrack):
    kind = "audio"

    def __init__(self):
        super().__init__()
        self._timestamp = 0

    async def recv(self):
        pts, time_base = self._timestamp, fractions.Fraction(1, 48000)
        self._timestamp += 960
        frame = av.AudioFrame(format="s16", layout="mono", samples=960)
        frame.pts = pts
        frame.sample_rate = 48000
        frame.time_base = time_base
        for p in frame.planes:
            p.update(b"\x00" * 1920)
        await asyncio.sleep(0.02)
        return frame

import fractions


async def test_session_payload_field(field_name: str, field_value: any) -> dict:
    """Test if /realtime/wm accepts a specific session_payload field."""
    logger.info("Testing session_payload field: %s", field_name)
    auth = ChatGPTAuthManager()
    token = auth.get_token()

    pc = RTCPeerConnection()
    track = DummyAudioTrack()
    pc.addTrack(track)
    dc = pc.createDataChannel("oai-events", id=0, negotiated=True)

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    sdp_offer = pc.localDescription.sdp

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
        "conversation_mode": {"kind": "primary_assistant"},
        "chat_mode": "chat",
        "backend_model": "auto",
        "enable_message_streaming": True,
    }
    if field_name:
        session_payload[field_name] = field_value

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
    mp.addpart(name="sdp", data=sdp_offer.encode("utf-8"))
    mp.addpart(name="session", data=json.dumps(session_payload).encode("utf-8"))

    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.post(url, headers=headers, multipart=mp, timeout=15)
        status = res.status_code
        body = res.text[:200]
        logger.info("Field %s -> status=%s body=%s", field_name, status, body)
        await pc.close()
        return {"field": field_name, "status": status, "body": body}

async def run_signaling_tests():
    fields_to_test = [
        ("instructions", PERSONA_PROMPT),
        ("custom_instructions", PERSONA_PROMPT),
        ("system_prompt", PERSONA_PROMPT),
        ("developer_instructions", PERSONA_PROMPT),
        ("base_instructions", PERSONA_PROMPT),
        ("initial_prompts", [{"role": "system", "content": PERSONA_PROMPT}]),
    ]
    results = []
    for f_name, f_val in fields_to_test:
        try:
            res = await test_session_payload_field(f_name, f_val)
            results.append(res)
        except Exception as e:
            logger.error("Error testing %s: %s", f_name, e)
            results.append({"field": f_name, "status": "error", "body": str(e)})
        await asyncio.sleep(1.0)
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    asyncio.run(run_signaling_tests())
