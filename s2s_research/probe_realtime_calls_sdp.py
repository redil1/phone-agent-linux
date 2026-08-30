import asyncio
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
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    sdp = pc.localDescription.sdp
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/sdp",
    }
    
    url = "https://api.openai.com/v1/realtime/calls"
    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.post(url, headers=headers, data=sdp, timeout=10)
        print(f"POST {url} -> Status: {res.status_code}")
        print(f"Response:\n{res.text[:400]}")
    
    await pc.close()

if __name__ == "__main__":
    asyncio.run(test())
