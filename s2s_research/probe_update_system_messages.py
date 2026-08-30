import asyncio
import json
from curl_cffi.requests import AsyncSession
from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager

async def set_persona_in_system_messages():
    auth = ChatGPTAuthManager()
    token = auth.get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    
    url = "https://chatgpt.com/backend-api/user_system_messages"
    payload = {
        "about_user_message": "Le correspondant est un prospect au téléphone pour un abonnement IPTV.",
        "about_model_message": (
            "Tu es Aziz, responsable commercial senior chez OXzoon IPTV. "
            "Tu es en appel téléphonique direct avec un client. "
            "Tu parles toujours en français clair, chaleureux et concis (1 à 2 phrases par tour). "
            "Tes tarifs : Essentiel = 10€/mois (1 écran), Famille = 15€/mois (3 écrans), Premium = 20€/mois (5 écrans). "
            "Ne dis JAMAIS 'what is on your mind' ni 'comment puis-je vous aider en tant qu'IA'. "
            "Reste à 100% dans ton rôle de commercial d'OXzoon."
        ),
        "name_user_message": "",
        "role_user_message": "",
        "traits_model_message": "",
        "other_user_message": "",
        "personality_type_selection": "professional",
        "disabled_tools": [],
        "enabled": True,
        "traits_enabled": True,
    }
    
    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.post(url, headers=headers, json=payload)
        print("Update status:", res.status_code)
        print("Response:", res.text[:300])

if __name__ == "__main__":
    asyncio.run(set_persona_in_system_messages())
