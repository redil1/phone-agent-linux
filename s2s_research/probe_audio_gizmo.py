"""Test sending audio to Gizmo Voice model and capturing its audio reply."""

import asyncio
import json
import logging
import math
import struct
import uuid
import fractions
import av
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, AudioStreamTrack
from curl_cffi.curl import CurlMime
from curl_cffi.requests import AsyncSession

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AudioGizmoTest")

GIZMO_ID = "g-6a8f57bb2a088191ae684a0352c27e9d"

class VoiceStimulusTrack(AudioStreamTrack):
    kind = "audio"

    def __init__(self):
        super().__init__()
        self._timestamp = 0
        self._frame_count = 0
        # Generate 1.5 seconds of 440Hz speech-like modulated tone to trigger VAD
        self._tone_frames = 75 # 75 * 20ms = 1.5s

    async def recv(self):
        pts, time_base = self._timestamp, fractions.Fraction(1, 48000)
        self._timestamp += 960
        self._frame_count += 1

        frame = av.AudioFrame(format="s16", layout="mono", samples=960)
        frame.pts = pts
        frame.sample_rate = 48000
        frame.time_base = time_base

        if 50 < self._frame_count < 125:
            # Generate modulated audio to trigger server VAD
            t = (self._frame_count - 50) * 0.02
            samples = np.array([
                int(12000 * math.sin(2 * math.pi * 300 * (t + i / 48000)))
                for i in range(960)
            ], dtype=np.int16)
            raw = samples.tobytes()
        else:
            raw = b"\x00" * 1920

        for p in frame.planes:
            p.update(raw)
        await asyncio.sleep(0.02)
        return frame


async def run_audio_test():
    auth = ChatGPTAuthManager()
    token = auth.get_token()

    pc = RTCPeerConnection()
    track = VoiceStimulusTrack()
    pc.addTrack(track)
    dc = pc.createDataChannel("oai-events", id=0, negotiated=True)

    received_audio_chunks = []

    @dc.on("open")
    def on_open():
        logger.info("DataChannel OPEN with Custom GPT (%s)!", GIZMO_ID)

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
            logger.info("DC Event: %s (data=%s)", ev_type, str(ev)[:150])
        except Exception as e:
            logger.warning("Parse error: %s", e)

    @pc.on("track")
    def on_track(remote_track):
        logger.info("Remote audio stream active: %s", remote_track.kind)
        asyncio.create_task(record_audio(remote_track))

    async def record_audio(rt):
        try:
            while True:
                f = await rt.recv()
                raw = f.to_ndarray().tobytes()
                samples = np.frombuffer(raw, dtype=np.int16)
                rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) if len(samples) > 0 else 0
                if rms > 100:
                    received_audio_chunks.append(rms)
                    logger.info(">>> ASSISTANT SPEAKING! RMS = %.1f (frames=%d)", rms, len(received_audio_chunks))
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

    await asyncio.sleep(10.0)
    logger.info("Test finished. Assistant spoken frames received: %d", len(received_audio_chunks))
    await pc.close()

if __name__ == "__main__":
    asyncio.run(run_audio_test())
