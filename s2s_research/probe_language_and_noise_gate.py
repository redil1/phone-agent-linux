import asyncio
import json
import numpy as np
import soxr
from aiortc import RTCPeerConnection, AudioStreamTrack
from curl_cffi.requests import AsyncSession
from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

class SimTrack(AudioStreamTrack):
    kind = "audio"
    def __init__(self):
        super().__init__()
        self._pts = 0
    async def recv(self):
        # Simulate noisy cellular downlink (RMS ~ 350)
        noise = (np.random.randn(960) * 350).astype(np.int16)
        
        # High noise gate: RMS < 600 -> zero
        rms = float(np.sqrt(np.mean(noise.astype(np.float32) ** 2)))
        if rms < 600.0:
            samples = np.zeros(960, dtype=np.int16)
        else:
            samples = noise

        import fractions, av
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = 48000
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, 48000)
        self._pts += 960
        await asyncio.sleep(0.02)
        return frame

async def test():
    auth = ChatGPTAuthManager()
    token = auth.get_token()

    pc = RTCPeerConnection()
    pc.addTrack(SimTrack())
    dc = pc.createDataChannel("oai-events")

    transcripts = []
    interrupt_count = 0

    @dc.on("open")
    def on_open():
        prompt = (
            "STRICT LANGUAGE MANDATE: Tu dois parler EXCLUSIVEMENT en Français (fr-FR). "
            "Il est strictement interdit de parler espagnol ou anglais. "
            "Tu es Aziz, Directeur Commercial chez OXzoon. "
            "Tarifs : Essentiel 10€, Famille 15€, Annuel 80€."
        )
        session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": prompt,
                "audio": {
                    "input": {
                        "transcription": {"model": "whisper-1"},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.85,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 800,
                            "create_response": True,
                            "interrupt_response": True
                        }
                    },
                    "output": {
                        "voice": "coral"
                    }
                }
            }
        }
        dc.send(json.dumps(session_update))

    @dc.on("message")
    def on_msg(msg):
        nonlocal interrupt_count
        ev = json.loads(msg)
        t = ev.get("type", "")
        if t == "session.updated":
            print("Session updated confirmed! Triggering greeting...")
            trigger = {
                "type": "response.create",
                "response": {
                    "instructions": "Le client a décroché. Prononce ton accroche en français : 'Bonjour, ici Aziz de chez OXzoon.'"
                }
            }
            dc.send(json.dumps(trigger))
        elif t == "input_audio_buffer.speech_started":
            interrupt_count += 1
            print("[SPEECH STARTED TRIGGERED]")
        elif t == "response.output_audio_transcript.delta":
            delta = ev.get("delta", "")
            transcripts.append(delta)
            print(delta, end="", flush=True)

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    url = "https://api.openai.com/v1/realtime/calls?model=gpt-realtime-1.5"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/sdp",
    }

    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.post(url, headers=headers, data=pc.localDescription.sdp, timeout=15)
        if res.status_code == 201:
            from aiortc import RTCSessionDescription
            await pc.setRemoteDescription(RTCSessionDescription(sdp=res.text, type="answer"))

    await asyncio.sleep(6.0)
    print("\n\nFINAL RESULT:")
    print("Spoken:", "".join(transcripts))
    print(f"Interrupt count on noisy cellular line: {interrupt_count}")
    await pc.close()

if __name__ == "__main__":
    asyncio.run(test())
