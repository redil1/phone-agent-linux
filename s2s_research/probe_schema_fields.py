import asyncio
import json

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

    @dc.on("open")
    def on_open():
        print("DataChannel open! Testing session.update payload variations...")
        
        # Variation A
        payload_a = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": "Tu es Aziz. Parle uniquement en français.",
                "audio": {
                    "output": {
                        "voice": "coral"
                    }
                }
            }
        }
        dc.send(json.dumps(payload_a))

    @dc.on("message")
    def on_msg(msg):
        ev = json.loads(msg)
        print("Received:", json.dumps(ev, indent=2))

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
