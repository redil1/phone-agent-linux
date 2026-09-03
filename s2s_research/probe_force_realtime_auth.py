import asyncio

from curl_cffi.requests import AsyncSession

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager


async def probe_realtime_endpoints():
    auth = ChatGPTAuthManager()
    token = auth.get_token()
    
    headers_standard = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "realtime=v1",
    }
    
    endpoints = [
        ("POST", "https://api.openai.com/v1/realtime/sessions", headers_standard, {"model": "gpt-4o-realtime-preview-2024-12-17", "voice": "coral"}),
        ("POST", "https://chatgpt.com/backend-api/realtime/sessions", headers_standard, {"model": "gpt-4o-realtime-preview-2024-12-17", "voice": "coral"}),
        ("POST", "https://chatgpt.com/backend-api/f/realtime/sessions", headers_standard, {"model": "gpt-4o-realtime-preview-2024-12-17", "voice": "coral"}),
    ]
    
    for method, url, hdrs, payload in endpoints:
        async with AsyncSession(impersonate="safari17_0") as session:
            try:
                res = await session.post(url, headers=hdrs, json=payload, timeout=10)
                print(f"{method} {url} -> Status: {res.status_code}")
                print(f"  Response: {res.text[:250]}\n")
            except Exception as exc:
                print(f"{method} {url} -> Exception: {exc}\n")

if __name__ == "__main__":
    asyncio.run(probe_realtime_endpoints())
