import asyncio
import json
import uuid
from curl_cffi.requests import AsyncSession
from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

GIZMO_ID = "g-6a8f59bfdfc8819191e85d3cfa8fd722"

async def test_create_thread():
    auth = ChatGPTAuthManager()
    token = auth.get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    
    msg_id = str(uuid.uuid4())
    parent_id = str(uuid.uuid4())
    payload = {
        "action": "next",
        "messages": [
            {
                "id": msg_id,
                "author": {"role": "user"},
                "content": {
                    "content_type": "text",
                    "parts": ["Tu es prêt pour l'appel commercial OXzoon IPTV."]
                },
                "metadata": {}
            }
        ],
        "parent_message_id": parent_id,
        "model": "auto",
        "timezone_offset_min": -60,
        "history_and_training_disabled": False,
        "conversation_mode": {
            "kind": "gizmo_interaction",
            "gizmo_id": GIZMO_ID
        }
    }
    
    url = "https://chatgpt.com/backend-api/conversation"
    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.post(url, headers=headers, json=payload, timeout=15)
        print(f"Status: {res.status_code}")
        # Parse SSE lines to find conversation_id
        for line in res.text.split("\n"):
            if line.startswith("data: ") and "conversation_id" in line:
                try:
                    data = json.loads(line[6:])
                    conv_id = data.get("conversation_id")
                    if conv_id:
                        print("FOUND CONVERSATION_ID:", conv_id)
                        return conv_id
                except Exception:
                    pass

if __name__ == "__main__":
    asyncio.run(test_create_thread())
