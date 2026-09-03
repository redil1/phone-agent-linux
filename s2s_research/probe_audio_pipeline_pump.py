import asyncio
import json

import numpy as np
import soxr
from aiortc import AudioStreamTrack, RTCPeerConnection
from curl_cffi.requests import AsyncSession

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager


class DummyTrack(AudioStreamTrack):
    kind = "audio"

async def test():
    auth = ChatGPTAuthManager()
    token = auth.get_token()

    pc = RTCPeerConnection()
    pc.addTrack(DummyTrack())
    dc = pc.createDataChannel("oai-events")

    chunks_written = 0

    @pc.on("track")
    def on_track(track):
        asyncio.create_task(pump(track))

    async def pump(track):
        nonlocal chunks_written
        pcm_accumulator = bytearray()
        while True:
            frame = await track.recv()
            arr = frame.to_ndarray()
            # If stereo (2, N), average channels to mono (N,)
            if arr.ndim == 2 and arr.shape[0] == 2:
                mono = (arr[0].astype(np.float32) + arr[1].astype(np.float32)) * 0.5
            elif arr.ndim == 2 and arr.shape[0] == 1:
                mono = arr[0].astype(np.float32)
            else:
                mono = arr.astype(np.float32).flatten()

            resampled = soxr.resample(mono, frame.sample_rate, 16000, quality="HQ")
            pcm_16k = np.rint(np.clip(resampled, -32768, 32767)).astype(np.int16).tobytes()
            pcm_accumulator.extend(pcm_16k)

            while len(pcm_accumulator) >= 640:
                chunk = bytes(pcm_accumulator[:640])
                del pcm_accumulator[:640]
                chunks_written += 1

    @dc.on("open")
    def on_open():
        trigger = {
            "type": "response.create",
            "response": {
                "instructions": "Dis 'Bonjour ceci est un test audio direct' en français."
            }
        }
        dc.send(json.dumps(trigger))

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

    await asyncio.sleep(4.0)
    print(f"Total 20ms 16kHz phone audio chunks generated: {chunks_written}")
    await pc.close()

if __name__ == "__main__":
    asyncio.run(test())
