import asyncio

from curl_cffi.requests import AsyncSession

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager


async def test_formats():
    auth = ChatGPTAuthManager()
    token = auth.get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    
    url = "https://chatgpt.com/backend-api/user_system_messages"
    
    about_user = "Appel sortant de prospection commerciale IPTV OXzoon. Le client vient de décrocher."
    about_model = (
        "Tu es Aziz, responsable commercial chez OXzoon IPTV, un vendeur d'élite d'exception : proactif, chaleureux, ultra persuasif. "
        "IMPORTANT : C'est TOI qui passes cet appel sortant au client pour le prospecter. Le client n'a pas appelé. "
        "Dès qu'il décroche et dit Allô : salue avec énergie, présente-toi, annonce l'objet de ton appel (notre offre IPTV 4K sport & cinéma sans coupure) et pose une question d'accroche. "
        "Nos offres : Essentiel = 10€/mois (1 écran), Famille = 15€/mois (3 écrans), Premium = 20€/mois (5 écrans). "
        "Ne dis jamais 'en quoi puis-je vous aider' car c'est toi qui l'appelles. Mène toujours la discussion et propose un test ou l'activation."
    )
    
    payload = {
        "about_user_message": about_user,
        "about_model_message": about_model,
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
        print("Status:", res.status_code)
        saved = res.json()
        print("Saved about_user:", saved.get("about_user_message"))
        print("Saved about_model:", saved.get("about_model_message"))

if __name__ == "__main__":
    asyncio.run(test_formats())
