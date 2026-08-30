import asyncio
from curl_cffi.requests import AsyncSession
from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

async def test_developer_webrtc():
    auth = ChatGPTAuthManager()
    token = auth.get_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/sdp",
        "OpenAI-Beta": "realtime=v1",
    }
    
    url = "https://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"
    dummy_sdp = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
    
    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.post(url, headers=headers, data=dummy_sdp, timeout=10)
        print(f"POST {url} -> Status: {res.status_code}")
        print(f"Response: {res.text[:300]}")

if __name__ == "__main__":
    asyncio.run(test_developer_webrtc())
