import asyncio

from curl_cffi.requests import AsyncSession

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager


async def main():
    auth = ChatGPTAuthManager()
    token = auth.get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    url = "https://chatgpt.com/backend-api/gizmos/bootstrap"
    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.get(url, headers=headers)
        data = res.json()
        print(f"Status: {res.status_code}")
        for item in data.get("gizmos", []):
            g = item.get("resource", {}).get("gizmo", {})
            print(f"- ID: {g.get('id')} | Name: {g.get('display', {}).get('name')} | Description: {g.get('display', {}).get('description')}")

if __name__ == "__main__":
    asyncio.run(main())
