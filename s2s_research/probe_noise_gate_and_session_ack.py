import asyncio
import fractions
import json

import av
import numpy as np
from aiortc import AudioStreamTrack, RTCPeerConnection
from curl_cffi.requests import AsyncSession

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager
from phone_agent_gateway.ai_bridge.personality.persona_compiler import PersonaCompiler
from phone_agent_gateway.ai_bridge.tasks.task_engine import TaskEngine


class NoiseGatedTrack(AudioStreamTrack):
    kind = "audio"
    def __init__(self):
        super().__init__()
        self._pts = 0
    async def recv(self):
        # Simulate cellular background hiss (noise level RMS ~ 120)
        noise = (np.random.randn(960) * 120).astype(np.int16)
        
        # Noise gate: if RMS < 250, send pure digital silence!
        rms = float(np.sqrt(np.mean(noise.astype(np.float32) ** 2)))
        if rms < 250:
            samples = np.zeros(960, dtype=np.int16)
        else:
            samples = noise

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

    compiler = PersonaCompiler()
    task_engine = TaskEngine()
    contract = task_engine.require_contract("iptv_subscription_sales")
    system_prompt = compiler.compile(task_contract=contract, language="fr-FR")

    pc = RTCPeerConnection()
    pc.addTrack(NoiseGatedTrack())
    dc = pc.createDataChannel("oai-events")

    session_updated_event = asyncio.Event()
    speech_started_count = 0
    spoken_text = []

    @dc.on("open")
    def on_open():
        print("DataChannel open! Sending session.update with threshold=0.8...")
        session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": system_prompt,
                "audio": {
                    "input": {
                        "transcription": {"model": "whisper-1"},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.8,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 600,
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
        nonlocal speech_started_count
        try:
            ev = json.loads(msg)
            t = ev.get("type", "")
            if t == "session.updated":
                print(">>> SESSION.UPDATED CONFIRMED BY OPENAI <<<")
                session_updated_event.set()
            elif t == "input_audio_buffer.speech_started":
                speech_started_count += 1
                print(f"[VAD SPEECH STARTED #{speech_started_count}]")
            elif t == "response.output_audio_transcript.delta":
                delta = ev.get("delta", "")
                spoken_text.append(delta)
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
        if res.status_code == 201:
            from aiortc import RTCSessionDescription
            await pc.setRemoteDescription(RTCSessionDescription(sdp=res.text, type="answer"))

    # Wait for session.updated confirmation
    await asyncio.wait_for(session_updated_event.wait(), timeout=5.0)
    print("\nTriggering opening greeting now that session is 100% updated...")

    trigger = {
        "type": "response.create",
        "response": {
            "instructions": "Le client vient de décrocher son téléphone et dit 'Allô ?'. Prononce ton message d'accroche commercial."
        }
    }
    dc.send(json.dumps(trigger))

    await asyncio.sleep(6.0)
    print("\n\nSpoken Text:", "".join(spoken_text))
    print(f"Total VAD speech started false triggers on background noise: {speech_started_count}")
    await pc.close()

if __name__ == "__main__":
    asyncio.run(test())
