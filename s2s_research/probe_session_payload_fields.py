import asyncio
import json
import uuid
import fractions
import av
from aiortc import RTCPeerConnection, RTCSessionDescription, AudioStreamTrack
from curl_cffi.curl import CurlMime
from curl_cffi.requests import AsyncSession

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

class DummyTrack(AudioStreamTrack):
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

async def test_session_fields():
    auth = ChatGPTAuthManager()
    token = auth.get_token()

    field_candidates = [
        {"instructions": "You are Aziz from OXzoon IPTV."},
        {"system_message": "You are Aziz from OXzoon IPTV."},
        {"system_prompt": "You are Aziz from OXzoon IPTV."},
        {"custom_instructions": {"about_model": "You are Aziz from OXzoon IPTV."}},
        {"user_system_messages": {"about_model_message": "You are Aziz from OXzoon IPTV."}},
    ]

    for cand in field_candidates:
        pc = RTCPeerConnection()
        pc.addTrack(DummyTrack())
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        base_session = {
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
        base_session.update(cand)

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
        mp.addpart(name="session", data=json.dumps(base_session).encode("utf-8"))

        async with AsyncSession(impersonate="safari17_0") as session:
            res = await session.post(url, headers=headers, multipart=mp, timeout=10)
            key = list(cand.keys())[0]
            print(f"Testing session field [{key}] -> Status: {res.status_code}")

        await pc.close()

if __name__ == "__main__":
    asyncio.run(test_session_fields())
