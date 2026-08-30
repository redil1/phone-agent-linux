import asyncio
import json
import logging
import fractions
import av
from aiortc import RTCPeerConnection, RTCSessionDescription, AudioStreamTrack
from curl_cffi.requests import AsyncSession
from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("GARealtimeProbe")

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

async def test():
    auth = ChatGPTAuthManager()
    token = auth.get_token()

    pc = RTCPeerConnection()
    pc.addTrack(StableTrack())
    dc = pc.createDataChannel("oai-events")

    transcript_parts = []

    @dc.on("open")
    def on_open():
        logger.info("GA DataChannel open! Sending session.update...")
        session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": (
                    "Tu es Aziz, Directeur Commercial chez OXzoon IPTV. "
                    "Tu es un vendeur d'exception, très chaleureux et directif. "
                    "C'est un appel sortant que tu passes au client. "
                    "Quand le client dit Allô, salue-le en français, présente-toi comme Aziz d'OXzoon "
                    "et propose nos forfaits IPTV 4K : Essentiel 10€/mois, Famille 15€/mois, Premium 20€/mois."
                ),
                "voice": "coral"
            }
        }
        dc.send(json.dumps(session_update))

        trigger = {
            "type": "response.create",
            "response": {
                "instructions": "Dis ton message d'accroche commercial maintenant en français."
            }
        }
        dc.send(json.dumps(trigger))

    @dc.on("message")
    def on_msg(msg):
        try:
            ev = json.loads(msg)
            t = ev.get("type", "")
            if t == "response.output_audio_transcript.delta":
                delta = ev.get("delta", "")
                transcript_parts.append(delta)
                print(delta, end="", flush=True)
            elif t == "response.output_audio_transcript.done":
                print("\n")
            elif t == "error":
                logger.error("Error event: %s", ev)
            elif t == "session.updated":
                logger.info("Session updated successfully confirmed by OpenAI!")
        except Exception:
            pass

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    url = "https://api.openai.com/v1/realtime/calls?model=gpt-realtime-1.5"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/sdp",
    }

    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.post(url, headers=headers, data=pc.localDescription.sdp, timeout=15)
        logger.info("SDP Status: %s", res.status_code)
        if res.status_code == 201:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=res.text, type="answer"))

    await asyncio.sleep(6.0)
    print("Spoken by model:", "".join(transcript_parts))
    await pc.close()

if __name__ == "__main__":
    asyncio.run(test())
