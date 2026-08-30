import asyncio
import json
import fractions
import av
from aiortc import RTCPeerConnection, RTCSessionDescription, AudioStreamTrack
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

    @pc.on("track")
    def on_track(track):
        print("Remote track received! kind:", track.kind)
        asyncio.create_task(read_track(track))

    async def read_track(track):
        count = 0
        while count < 10:
            frame = await track.recv()
            count += 1
            print(f"Frame #{count}: rate={frame.sample_rate}, format={frame.format.name}, layout={frame.layout.name}, samples={frame.samples}, pts={frame.pts}")

    @dc.on("open")
    def on_open():
        trigger = {
            "type": "response.create",
            "response": {
                "instructions": "Dis 'Un deux trois test' en français."
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
    await pc.close()

if __name__ == "__main__":
    asyncio.run(test())
