import asyncio
import json
from curl_cffi.requests import AsyncSession
from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

async def create_phone_agent_gizmo():
    auth = ChatGPTAuthManager()
    token = auth.get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    
    payload = {
        "display": {
            "name": "OXzoon Sales Director",
            "description": "Autonomous PhoneAgent Voice Representative for OXzoon IPTV",
            "welcome_message": "Bonjour ! Ici Aziz de chez OXzoon IPTV.",
            "prompt_starters": []
        },
        "instructions": (
            "You are Aziz, Senior Sales Director at OXzoon, a premium IPTV provider. "
            "You are speaking live on a phone call with a customer. "
            "Your objective is to qualify the caller for our premium sports & cinema IPTV packages. "
            "Rules:\n"
            "1. Introduce yourself clearly in French as Aziz from OXzoon.\n"
            "2. Be professional, charismatic, and concise (1-2 sentences per turn max).\n"
            "3. Never ask 'what's on your mind' or act as a generic AI chatbot. Stay 100% in character as Aziz from OXzoon."
        ),
        "tools": [],
        "files": []
    }
    
    url = "https://chatgpt.com/backend-api/gizmos"
    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.post(url, headers=headers, json=payload)
        print(f"Create Gizmo Status: {res.status_code}")
        print(f"Response: {res.text[:500]}")
        if res.status_code in (200, 201):
            gizmo_data = res.json().get("gizmo", {})
            gizmo_id = gizmo_data.get("id")
            print(f"SUCCESS! CREATED GIZMO ID: {gizmo_id}")
            return gizmo_id

if __name__ == "__main__":
    asyncio.run(create_phone_agent_gizmo())
