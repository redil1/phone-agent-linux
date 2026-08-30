import asyncio
from curl_cffi.requests import AsyncSession
from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

async def test_realtime_calls():
    auth = ChatGPTAuthManager()
    token = auth.get_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    
    url = "https://api.openai.com/v1/realtime/calls"
    payload = {
        "model": "gpt-4o-realtime-preview",
        "instructions": "You are Aziz from OXzoon IPTV.",
    }
    
    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.post(url, headers=headers, json=payload, timeout=10)
        print(f"POST {url} -> Status: {res.status_code}")
        print(f"Response: {res.text[:300]}")

if __name__ == "__main__":
    asyncio.run(test_realtime_calls())
