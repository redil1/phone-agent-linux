import asyncio
import json
import logging
import uuid
import fractions
import av
from aiortc import RTCPeerConnection, RTCSessionDescription, AudioStreamTrack
from curl_cffi.requests import AsyncSession

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("GPTRealtimeDeveloperProbe")

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

async def test_developer_realtime():
    auth = ChatGPTAuthManager()
    token = auth.get_token()

    pc = RTCPeerConnection()
    pc.addTrack(StableTrack())
    dc = pc.createDataChannel("oai-events")

    events_received = []

    @dc.on("open")
    def on_open():
        logger.info("Developer Realtime DataChannel OPEN!")
        
        # 1. Update session with exact persona and instructions
        session_update = {
            "type": "session.update",
            "session": {
                "modalities": ["audio", "text"],
                "instructions": (
                    "Tu es Aziz, Directeur Commercial chez OXzoon IPTV, le meilleur vendeur au monde : "
                    "chaleureux, dynamique, ultra persuasif et proactif. "
                    "CONTEXTE : C'est TOI qui passes cet appel sortant de prospection au client. "
                    "Dès qu'il décroche et dit Allô : salue avec énergie, présente-toi, "
                    "annonce l'objet de ton appel (nos abonnements IPTV 4K sport et films sans coupure) "
                    "et pose une question d'accroche. "
                    "Tarifs : Essentiel = 10€/mois (1 écran), Famille = 15€/mois (3 écrans), Premium = 20€/mois (5 écrans). "
                    "Qualifie ses écrans, pitche l'offre, réponds aux objections avec assurance "
                    "et propose un test immédiat de 24h ou l'activation. "
                    "Reste concis (1 à 2 phrases max par tour) et mène toujours l'échange."
                ),
                "voice": "coral",
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 500
                }
            }
        }
        dc.send(json.dumps(session_update))
        logger.info("Sent session.update!")

        # 2. Trigger initial opening speech
        trigger = {
            "type": "response.create",
            "response": {
                "instructions": "Le client vient de décrocher son téléphone et a dit 'Allô ?'. Prononce ton message d'accroche commercial."
            }
        }
        dc.send(json.dumps(trigger))
        logger.info("Sent response.create trigger!")

    @dc.on("message")
    def on_msg(msg):
        try:
            ev = json.loads(msg)
            t = ev.get("type", "")
            events_received.append(t)
            logger.info("Realtime Event: %s", t)
            if t == "response.audio_transcript.delta":
                print(ev.get("delta", ""), end="", flush=True)
            elif t == "response.audio_transcript.done":
                print("\n[TRANSCRIPT DONE]")
            elif t == "error":
                logger.error("Realtime ERROR: %s", json.dumps(ev, indent=2))
        except Exception as exc:
            logger.debug("Error parsing: %s", exc)

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    url = "https://api.openai.com/v1/realtime/calls?model=gpt-realtime-1.5"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/sdp",
    }

    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.post(url, headers=headers, data=pc.localDescription.sdp, timeout=15)
        logger.info("SDP Handshake Status: %s", res.status_code)
        if res.status_code == 201:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=res.text, type="answer"))

    await asyncio.sleep(8.0)
    logger.info("All events received: %s", events_received)
    await pc.close()

if __name__ == "__main__":
    asyncio.run(test_developer_realtime())
