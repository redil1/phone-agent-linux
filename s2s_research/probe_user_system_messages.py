import asyncio
import json
from curl_cffi.requests import AsyncSession
from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

async def inspect_user_system_messages():
    auth = ChatGPTAuthManager()
    token = auth.get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    
    url = "https://chatgpt.com/backend-api/user_system_messages"
    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.get(url, headers=headers)
        print("Status:", res.status_code)
        print(json.dumps(res.json(), indent=2))

if __name__ == "__main__":
    asyncio.run(inspect_user_system_messages())
