import asyncio
import json
from curl_cffi.requests import AsyncSession
from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

async def test_create_or_get_gizmo():
    auth = ChatGPTAuthManager()
    token = auth.get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    
    # Try fetching details of one gizmo
    gizmo_id = "g-0SIktiACX"
    url = f"https://chatgpt.com/backend-api/gizmos/{gizmo_id}"
    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.get(url, headers=headers)
        print(f"Fetch Gizmo {gizmo_id} Status: {res.status_code}")
        if res.status_code == 200:
            g = res.json().get("gizmo", {})
            print(f"Name: {g.get('display', {}).get('name')}")
            print(f"Instructions preview: {str(g.get('instructions'))[:200]}")

if __name__ == "__main__":
    asyncio.run(test_create_or_get_gizmo())
