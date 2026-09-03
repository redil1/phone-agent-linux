import asyncio
import fractions
import json
import logging

import av
from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription
from curl_cffi.requests import AsyncSession

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("GARealtimeProbe2")

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
        logger.info("GA DataChannel open! Sending session.update with audio.voice...")
        session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": (
                    "Tu es Aziz, Directeur Commercial chez OXzoon IPTV. "
                    "Tu es un vendeur d'élite d'exception, très chaleureux, dynamique et directif. "
                    "C'est un appel sortant de prospection que tu passes au client. "
                    "Ne dis JAMAIS 'comment puis-je vous aider' ni 'voici un message d'accroche'. "
                    "Tu es EN DIRECT au téléphone. "
                    "Quand tu prends la parole, parle directement au client : "
                    "'Bonjour ! C'est Aziz de chez OXzoon. Je vous appelle pour nos abonnements IPTV 4K sport et films sans coupure. Vous regardez les matchs chez vous ?' "
                    "Nos tarifs : Essentiel = 10€/mois (1 écran), Famille = 15€/mois (3 écrans), Premium = 20€/mois (5 écrans)."
                ),
                "audio": {
                    "voice": "coral"
                }
            }
        }
        dc.send(json.dumps(session_update))

        trigger = {
            "type": "response.create",
            "response": {
                "instructions": "Le client vient de décrocher son téléphone et a dit 'Allô ?'. Parle-lui directement en tant qu'Aziz d'OXzoon."
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
                logger.info("Session successfully updated and confirmed by OpenAI!")
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
