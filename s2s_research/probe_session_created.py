import asyncio
import json
from aiortc import RTCPeerConnection, AudioStreamTrack
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

    @dc.on("open")
    def on_open():
        pass

    @dc.on("message")
    def on_msg(msg):
        try:
            ev = json.loads(msg)
            if ev.get("type") == "session.created":
                print("SESSION CREATED SCHEMA:\n", json.dumps(ev, indent=2))
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

    await asyncio.sleep(4.0)
    await pc.close()

if __name__ == "__main__":
    asyncio.run(test())
