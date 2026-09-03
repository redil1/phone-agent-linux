import asyncio

from curl_cffi.requests import AsyncSession

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

GIZMO_ID = "g-6a8f59bfdfc8819191e85d3cfa8fd722"

async def test_gizmo_endpoints():
    auth = ChatGPTAuthManager()
    token = auth.get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    
    endpoints = [
        ("GET", f"https://chatgpt.com/backend-api/gizmos/{GIZMO_ID}"),
        ("GET", f"https://chatgpt.com/backend-api/gizmos/{GIZMO_ID}/conversations?cursor=0&limit=5"),
        ("POST", f"https://chatgpt.com/backend-api/gizmos/{GIZMO_ID}/conversations"),
        ("GET", "https://chatgpt.com/backend-api/user_system_messages"),
    ]
    
    for method, url in endpoints:
        async with AsyncSession(impersonate="safari17_0") as session:
            if method == "GET":
                res = await session.get(url, headers=headers)
            else:
                res = await session.post(url, headers=headers, json={})
            print(f"{method} {url} -> Status: {res.status_code} Body: {res.text[:150]}")

if __name__ == "__main__":
    asyncio.run(test_gizmo_endpoints())
