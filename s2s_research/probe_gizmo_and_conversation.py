"""Test Gizmo (Custom GPT) mode and Conversation ID binding with ChatGPT WebRTC."""

import asyncio
import fractions
import json
import logging
import uuid

import av
from aiortc import AudioStreamTrack, RTCPeerConnection
from curl_cffi.curl import CurlMime
from curl_cffi.requests import AsyncSession

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("GizmoTest")

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


async def test_gizmo_mode(gizmo_id: str):
    logger.info("Testing Gizmo Mode with gizmo_id=%s", gizmo_id)
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
        "conversation_mode": {
            "kind": "gizmo_interaction",
            "gizmo_id": gizmo_id
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
    mp.addpart(name="sdp", data=sdp_offer.encode("utf-8"))
    mp.addpart(name="session", data=json.dumps(session_payload).encode("utf-8"))

    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.post(url, headers=headers, multipart=mp, timeout=15)
        logger.info("Gizmo signaling response: status=%s body=%s", res.status_code, res.text[:200])
        await pc.close()
        return res.status_code == 201


async def list_user_gizmos():
    auth = ChatGPTAuthManager()
    token = auth.get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    url = "https://chatgpt.com/backend-api/gizmos/bootstrap"
    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.get(url, headers=headers)
        logger.info("Bootstrap gizmos: status=%s body=%s", res.status_code, res.text[:300])

if __name__ == "__main__":
    asyncio.run(list_user_gizmos())
    asyncio.run(test_gizmo_mode("g-p-681092963c3c8191b0537f71472c47a4"))
