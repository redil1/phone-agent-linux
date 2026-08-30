"""Test live WebRTC Voice session connected directly to our compiled Custom GPT."""

import asyncio
import json
import logging
import uuid
import fractions
import av
from aiortc import RTCPeerConnection, RTCSessionDescription, AudioStreamTrack
from curl_cffi.curl import CurlMime
from curl_cffi.requests import AsyncSession

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("LiveGizmoVoiceTest")

GIZMO_ID = "g-6a8f57bb2a088191ae684a0352c27e9d"

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


async def test_gizmo_voice_session():
    auth = ChatGPTAuthManager()
    token = auth.get_token()

    pc = RTCPeerConnection()
    track = DummyAudioTrack()
    pc.addTrack(track)
    dc = pc.createDataChannel("oai-events", id=0, negotiated=True)

    transcript_collector = []

    @dc.on("open")
    def on_open():
        logger.info("DataChannel OPEN with Custom GPT (%s)!", GIZMO_ID)
        # Send greeting prompt
        event = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Allô ?"}]
            }
        }
        dc.send(json.dumps(event))
        dc.send(json.dumps({"type": "response.create"}))

    @dc.on("message")
    def on_msg(message: str):
        try:
            ev = json.loads(message)
            if ev.get("type") == "data_message" and isinstance(ev.get("data"), str):
                try:
                    inner = json.loads(ev["data"])
                    if isinstance(inner, dict):
                        ev = inner
                except Exception:
                    pass

            ev_type = ev.get("type") or ev.get("payload", {}).get("type")
            if ev_type == "response.audio_transcript.delta":
                delta = ev.get("delta", "")
                if delta:
                    transcript_collector.append(delta)
                    print(delta, end="", flush=True)
            elif ev_type == "response.audio_transcript.done":
                print("\n")
            logger.info("DC Event: %s", ev_type)
        except Exception as e:
            logger.warning("Parse error: %s", e)

    @pc.on("track")
    def on_track(remote_track):
        logger.info("Remote audio stream active: %s", remote_track.kind)
        asyncio.create_task(drain_track(remote_track))

    async def drain_track(rt):
        try:
            while True:
                await rt.recv()
        except Exception:
            pass

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
    mp.addpart(name="sdp", data=sdp_offer.encode("utf-8"))
    mp.addpart(name="session", data=json.dumps(session_payload).encode("utf-8"))

    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.post(url, headers=headers, multipart=mp, timeout=15)
        logger.info("Signaling status: %s", res.status_code)
        if res.status_code in (200, 201):
            answer = RTCSessionDescription(sdp=res.text, type="answer")
            await pc.setRemoteDescription(answer)

    # Wait for the model's spoken response
    await asyncio.sleep(8.0)
    full_transcript = "".join(transcript_collector).strip()
    logger.info("=== FULL SPOKEN RESPONSE FROM GIZMO VOICE MODEL ===\n%s", full_transcript)
    await pc.close()

if __name__ == "__main__":
    asyncio.run(test_gizmo_voice_session())
