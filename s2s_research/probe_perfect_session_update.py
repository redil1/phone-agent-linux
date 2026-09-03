import asyncio
import fractions
import json

import av
from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription
from curl_cffi.requests import AsyncSession

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager


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
        print("DataChannel OPEN!")
        session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": (
                    "Tu es Aziz, Directeur Commercial chez OXzoon IPTV, le meilleur vendeur au monde : "
                    "chaleureux, dynamique, ultra persuasif et proactif. "
                    "CONTEXTE CRUCIAL : C'est TOI qui passes cet appel sortant de prospection au client. "
                    "Le client n'a pas appelé. Ne dis jamais 'en quoi puis-je vous aider' ni 'que puis-je faire pour vous'. "
                    "Dès qu'il décroche et dit Allô : salue avec énergie, présente-toi, annonce l'objet de ton appel "
                    "(nos abonnements IPTV 4K sport et cinéma sans coupure) et pose une question d'accroche. "
                    "Tarifs : Essentiel = 10€/mois (1 écran), Famille = 15€/mois (3 écrans), Premium = 20€/mois (5 écrans). "
                    "Qualifie ses écrans, pitche l'offre adaptée, réponds aux objections avec assurance "
                    "et propose un test immédiat de 24h ou l'activation. "
                    "Reste concis (1 à 2 phrases max par tour) et mène toujours l'échange comme un grand commercial."
                ),
                "audio": {
                    "input": {
                        "transcription": {"model": "whisper-1"}
                    },
                    "output": {
                        "voice": "coral"
                    }
                }
            }
        }
        dc.send(json.dumps(session_update))

        trigger = {
            "type": "response.create",
            "response": {
                "instructions": "Le client vient de décrocher son téléphone et a dit 'Allô ?'. Prononce ton accroche commerciale d'Aziz d'OXzoon."
            }
        }
        dc.send(json.dumps(trigger))

    @dc.on("message")
    def on_msg(msg):
        try:
            ev = json.loads(msg)
            t = ev.get("type", "")
            if t == "session.updated":
                print("\n>>> OPENAI CONFIRMED SESSION.UPDATED! Persona bound perfectly! <<<")
            elif t == "response.output_audio_transcript.delta":
                delta = ev.get("delta", "")
                transcript_parts.append(delta)
                print(delta, end="", flush=True)
            elif t == "error":
                print("\nERROR:", ev)
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
        print("SDP Status:", res.status_code)
        if res.status_code == 201:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=res.text, type="answer"))

    await asyncio.sleep(6.0)
    print("\n\nFINAL SPOKEN OUTPUT:", "".join(transcript_parts))
    await pc.close()

if __name__ == "__main__":
    asyncio.run(test())
