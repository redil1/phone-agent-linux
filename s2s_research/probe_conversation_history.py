import asyncio
import json
from curl_cffi.requests import AsyncSession
from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

async def main():
    auth = ChatGPTAuthManager()
    token = auth.get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    url = "https://chatgpt.com/backend-api/conversation/6a8f368d-a644-83e9-98f6-02c75ce2bd91"
    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.get(url, headers=headers)
        print(f"Status: {res.status_code}")
        if res.status_code == 200:
            data = res.json()
            print("Mapping keys:", list(data.get("mapping", {}).keys()))
            for node_id, node in data.get("mapping", {}).items():
                msg = node.get("message")
                if msg:
                    role = msg.get("author", {}).get("role")
                    content = msg.get("content", {})
                    parts = content.get("parts", [])
                    print(f"- Role: {role} | Content: {str(parts)[:120]}")

if __name__ == "__main__":
    asyncio.run(main())
