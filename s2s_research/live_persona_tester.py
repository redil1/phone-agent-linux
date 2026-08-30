"""Automated live persona tester for ChatGPT Realtime WebRTC Voice.

Connects with different instruction injection strategies and records the assistant's
opening transcript to verify which strategy makes the voice AI adopt the exact persona.
"""

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
logger = logging.getLogger("LivePersonaTest")

STRICT_PERSONA = (
    "You are Aziz, the Senior Sales Director at OXzoon IPTV. "
    "You are on a live phone call with a customer. "
    "Introduce yourself immediately and ask if they are looking for the premium sports package. "
    "Do not say 'what is on your mind' or ask casual questions. Speak only as Aziz from OXzoon in French: "
    "'Bonjour ! Ici Aziz, Directeur Commercial chez OXzoon IPTV. Êtes-vous intéressé par notre bouquet sport premium ?'"
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


async def run_strategy_test(name: str, payload_mods: dict, on_dc_open_actions: list = None, timeout_secs: float = 12.0) -> str:
    logger.info("=== STARTING TEST: %s ===", name)
    auth = ChatGPTAuthManager()
    token = auth.get_token()

    pc = RTCPeerConnection()
    track = DummyAudioTrack()
    pc.addTrack(track)
    dc = pc.createDataChannel("oai-events", id=0, negotiated=True)

    transcript_collector = []
    received_events = []
    ready_evt = asyncio.Event()

    @dc.on("open")
    def on_open():
        logger.info("[%s] DataChannel open!", name)
        ready_evt.set()
        if on_dc_open_actions:
            for action in on_dc_open_actions:
                logger.info("[%s] Sending DC action: %s", name, action.get("type"))
                dc.send(json.dumps(action))

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

            received_events.append(ev)
            ev_type = ev.get("type") or ev.get("payload", {}).get("type")

            # Collect transcript deltas
            if ev_type == "response.audio_transcript.delta":
                delta = ev.get("delta", "")
                if delta:
                    transcript_collector.append(delta)
            elif "delta" in ev:
                transcript_collector.append(ev.get("delta", ""))
            elif "text" in ev:
                transcript_collector.append(ev.get("text", ""))

            logger.info("[%s] DC Event: %s (payload=%s)", name, ev_type, str(ev.get("payload"))[:120])
        except Exception as e:
            logger.warning("[%s] Parse error: %s", name, e)

    @pc.on("track")
    def on_track(remote_track):
        logger.info("[%s] Remote audio track received: %s", name, remote_track.kind)
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
        "conversation_mode": {"kind": "primary_assistant"},
        "chat_mode": "chat",
        "backend_model": "auto",
        "enable_message_streaming": True,
    }
    session_payload.update(payload_mods)

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
        if res.status_code not in (200, 201):
            logger.error("[%s] Signaling error %s: %s", name, res.status_code, res.text)
            await pc.close()
            return f"Signaling failed: {res.status_code}"

        answer = RTCSessionDescription(sdp=res.text, type="answer")
        await pc.setRemoteDescription(answer)

    # Wait for transcript to be generated
    start_t = asyncio.get_running_loop().time()
    while asyncio.get_running_loop().time() - start_t < timeout_secs:
        await asyncio.sleep(0.5)
        if transcript_collector:
            # Let it finish the sentence
            await asyncio.sleep(2.0)
            break

    spoken_text = "".join(transcript_collector).strip()
    logger.info("=== RESULT [%s] Spoken Transcript: '%s' ===", name, spoken_text)
    await pc.close()
    return spoken_text


async def main():
    results = {}

    # Strategy 1: session_payload["instructions"]
    res1 = await run_strategy_test(
        "Strategy 1: HTTP session_payload['instructions']",
        {"instructions": STRICT_PERSONA}
    )
    results["Strategy 1"] = res1
    await asyncio.sleep(2.0)

    # Strategy 2: session_payload["custom_instructions"]
    res2 = await run_strategy_test(
        "Strategy 2: HTTP session_payload['custom_instructions']",
        {"custom_instructions": STRICT_PERSONA}
    )
    results["Strategy 2"] = res2
    await asyncio.sleep(2.0)

    # Strategy 3: DC session.update with instructions
    res3 = await run_strategy_test(
        "Strategy 3: DC session.update",
        {},
        on_dc_open_actions=[
            {
                "type": "session.update",
                "session": {"instructions": STRICT_PERSONA, "voice": "coral"}
            },
            {"type": "response.create"}
        ]
    )
    results["Strategy 3"] = res3
    await asyncio.sleep(2.0)

    # Strategy 4: DC conversation.item.create with role: system
    res4 = await run_strategy_test(
        "Strategy 4: DC conversation.item.create (role=system)",
        {},
        on_dc_open_actions=[
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": STRICT_PERSONA}]
                }
            },
            {"type": "response.create"}
        ]
    )
    results["Strategy 4"] = res4
    await asyncio.sleep(2.0)

    # Strategy 5: DC conversation.item.create with user roleplay override
    res5 = await run_strategy_test(
        "Strategy 5: DC conversation.item.create (user roleplay command)",
        {},
        on_dc_open_actions=[
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": f"Instruction: You are Aziz from OXzoon IPTV on a phone call. Greet the customer right now in French with: 'Bonjour ! Ici Aziz, Directeur Commercial chez OXzoon IPTV. Comment puis-je vous aider ?'"
                    }]
                }
            },
            {"type": "response.create"}
        ]
    )
    results["Strategy 5"] = res5

    print("\n================ SUMMARY OF RESULTS ================")
    for strat, text in results.items():
        print(f"[*] {strat} -> '{text}'")


if __name__ == "__main__":
    asyncio.run(main())
