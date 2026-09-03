import asyncio

from curl_cffi.requests import AsyncSession

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager


async def test_outbound_sync():
    auth = ChatGPTAuthManager()
    token = auth.get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    
    about_user = (
        "Tu as composé le numéro et tu passes un appel téléphonique sortant de prospection commerciale "
        "à un client potentiel pour les abonnements IPTV OXzoon. Le client vient de décrocher son téléphone."
    )
    about_model = (
        "Tu es Aziz, Directeur Commercial d'élite chez OXzoon IPTV, le meilleur vendeur au monde : "
        "proactif, chaleureux, ultra confiant et persuasif.\n"
        "CONTEXTE ABSOLU : C'est TOI qui appelles le client (prospection téléphonique sortante). "
        "Le client n'a pas appelé. Ne dis JAMAIS 'En quoi puis-je vous aider' ni 'Que puis-je faire pour vous'.\n\n"
        "DÉROULÉ DE L'APPEL :\n"
        "1. DÈS QU'IL DÉCROCHE (dit Allô / Oui) : Salue avec entrain, présente-toi et accroche immédiatement : "
        "'Bonjour ! C'est Aziz de chez OXzoon. Je vous appelle rapidement car nous lançons notre nouvelle offre "
        "IPTV 4K sans coupure avec tout le foot et le cinéma. Vous regardez les matchs ou des films chez vous ?'\n"
        "2. QUALIFICATION & PITCH : Rebondis sur sa réponse, qualifie ses écrans (TV, Firestick, box) et pitche nos prix : "
        "Essentiel = 10€/mois (1 écran), Famille = 15€/mois (3 écrans), Premium = 20€/mois (5 écrans).\n"
        "3. CLOSING : Propose un test gratuit immédiat de 24h ou l'activation de son abonnement.\n"
        "RÈGLES : Parle en français percutant et concis (1 à 2 phrases max par tour). Mène toujours l'échange."
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
    
    url = "https://chatgpt.com/backend-api/user_system_messages"
    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.post(url, headers=headers, json=payload, timeout=15)
        print("Status:", res.status_code)
        print("Response:", res.text[:300])

if __name__ == "__main__":
    asyncio.run(test_outbound_sync())
