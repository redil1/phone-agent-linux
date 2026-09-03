import asyncio

from curl_cffi.requests import AsyncSession

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

GIZMO_ID = "g-6a8f59bfdfc8819191e85d3cfa8fd722"

async def create_conversation_in_gizmo():
    auth = ChatGPTAuthManager()
    token = auth.get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    
    # Try prepare endpoint
    url = "https://chatgpt.com/backend-api/f/conversation/prepare"
    payload = {
        "model": "auto",
        "conversation_mode": {
            "kind": "gizmo_interaction",
            "gizmo_id": GIZMO_ID
        }
    }
    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.post(url, headers=headers, json=payload)
        print("Prepare status:", res.status_code)
        print("Prepare response:", res.text[:300])

if __name__ == "__main__":
    asyncio.run(create_conversation_in_gizmo())
