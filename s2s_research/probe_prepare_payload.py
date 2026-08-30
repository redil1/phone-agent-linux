import asyncio
import json
import uuid
from curl_cffi.requests import AsyncSession
from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

async def test():
    auth = ChatGPTAuthManager()
    token = auth.get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    
    url = "https://chatgpt.com/backend-api/f/conversation/prepare"
    # Test different payload schemas
    payloads = [
        {},
        {"model": "auto"},
        {"conversation_id": None},
        {"conversation_id": None, "model": "auto"},
        {"conversation_mode": {"kind": "primary_assistant"}},
        {"conversation_mode": {"kind": "gizmo_interaction", "gizmo_id": "g-6a8f59bfdfc8819191e85d3cfa8fd722"}},
    ]
    for p in payloads:
        async with AsyncSession(impersonate="safari17_0") as session:
            res = await session.post(url, headers=headers, json=p)
            print(f"Payload: {p} -> Status: {res.status_code} Body: {res.text[:150]}")

if __name__ == "__main__":
    asyncio.run(test())
