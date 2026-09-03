"""Automated Custom GPT (Gizmo) Manager for ChatGPT Realtime S2S WebRTC.

Compiles and synchronizes the active Persona and Task Contract into an official
OpenAI Custom GPT (Gizmo) on ChatGPT backend, allowing the Realtime WebRTC voice
cluster to adopt the exact system persona and task rules via `gizmo_interaction`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from curl_cffi.requests import AsyncSession

from .chatgpt_realtime_auth import ChatGPTAuthManager

logger = logging.getLogger("ChatGPTGizmoManager")

DEFAULT_GIZMO_CACHE_PATH = Path.home() / ".cache" / "phone_agent_gizmo_cache.json"


class ChatGPTGizmoManager:
    """Manages the creation, caching, and synchronization of Custom GPTs for S2S voice calls."""

    def __init__(
        self,
        auth_manager: ChatGPTAuthManager | None = None,
        cache_path: Path | None = None,
    ) -> None:
        self.auth_manager = auth_manager or ChatGPTAuthManager()
        self.cache_path = cache_path or DEFAULT_GIZMO_CACHE_PATH
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, str] = self._load_cache()

    def _load_cache(self) -> dict[str, str]:
        if not self.cache_path.is_file():
            return {}
        try:
            with self.cache_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("Could not read gizmo cache: %s", exc)
            return {}

    def _save_cache(self) -> None:
        try:
            with self.cache_path.open("w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
        except Exception as exc:
            logger.warning("Could not save gizmo cache: %s", exc)

    def compute_signature(
        self,
        system_prompt: str,
        identity: dict[str, Any],
        task_id: str,
    ) -> str:
        """Compute a unique deterministic hash for the active persona + task configuration."""
        raw = f"{task_id}|{identity.get('name')}|{identity.get('role')}|{system_prompt}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def format_gizmo_instructions(
        self,
        system_prompt: str,
        persona_data: dict[str, Any],
        task_contract: dict[str, Any],
    ) -> str:
        """Format high-density instructions guaranteed to fit within OpenAI's 8000 char limit."""
        if len(system_prompt) <= 7500:
            return system_prompt

        identity = persona_data.get("identity", {})
        name = identity.get("name", "Adam")
        role = identity.get("role", "Sales Director")
        org = identity.get("organization", "OXzoon")
        obj = task_contract.get("objective", "")
        knowledge = task_contract.get("knowledge", {})
        knowledge_str = "\n".join(f"- {k}: {v}" for k, v in knowledge.items())
        slots = task_contract.get("inputs_required", [])
        slots_str = "\n".join(f"- {s.get('id')}: {s.get('question')}" for s in slots)

        lines = [
            f"You are {name}, {role} at {org}.",
            "You are speaking live on a phone call with a customer.",
            f"Objective: {obj}",
            "\nKnowledge Base & Ground-Truth Pricing:",
            knowledge_str,
            "\nRequired Information to Collect:",
            slots_str,
            "\nRules of Engagement:",
            "1. Speak in concise, natural spoken sentences (max 1-2 sentences per turn).",
            "2. Never invent pricing, specs, or policies not listed above.",
            "3. Never ask 'what is on your mind' or ask generic AI questions. Stay in character.",
            "4. Acknowledge customer responses warmly and guide them through the sales process.",
        ]
        return "\n".join(lines)[:7500]

    async def get_or_create_gizmo(
        self,
        system_prompt: str,
        persona_data: dict[str, Any],
        task_contract: dict[str, Any],
        language: str = "fr-FR",
    ) -> str | None:
        """Retrieve cached Gizmo ID or create a new Custom GPT on ChatGPT backend."""
        identity = persona_data.get("identity", {})
        task_id = task_contract.get("id", "default_task")
        instructions = self.format_gizmo_instructions(
            system_prompt, persona_data, task_contract
        )
        signature = self.compute_signature(instructions, identity, task_id)

        if signature in self._cache:
            gizmo_id = self._cache[signature]
            logger.info(
                "Found cached Custom GPT for persona+task: %s (%s)",
                signature[:8],
                gizmo_id,
            )
            return gizmo_id

        logger.info(
            "No cached Custom GPT found. Creating new Custom GPT on ChatGPT for task=%s...",
            task_id,
        )
        try:
            token = await asyncio.to_thread(self.auth_manager.get_token)
        except Exception as exc:
            logger.warning("Could not get auth token for Gizmo creation: %s", exc)
            return None

        persona_name = identity.get("name", "PhoneAgent Representative")
        task_title = task_contract.get("title", task_id.replace("_", " ").title())
        openings = task_contract.get("opening_greeting", {})
        open_key = "fr" if language.lower().startswith("fr") else "en"
        welcome_msg = str(openings.get(open_key, f"Bonjour, ici {persona_name}."))[:100]

        payload = {
            "display": {
                "name": f"{persona_name} - {task_title}"[:50],
                "description": (
                    f"Autonomous PhoneAgent Voice Representative for {task_title}"[:100]
                ),
                "welcome_message": welcome_msg,
                "prompt_starters": [],
            },
            "instructions": instructions,
            "tools": [],
            "files": [],
        }

        url = "https://chatgpt.com/backend-api/gizmos"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }

        try:
            async with AsyncSession(impersonate="safari17_0") as session:
                res = await session.post(url, headers=headers, json=payload, timeout=20)
                if res.status_code in (200, 201):
                    gizmo_data = res.json().get("gizmo", {})
                    gizmo_id = gizmo_data.get("id")
                    if gizmo_id:
                        logger.info(
                            "Registered Custom GPT on OpenAI: %s (%s)",
                            payload["display"]["name"],
                            gizmo_id,
                        )
                        self._cache[signature] = gizmo_id
                        self._save_cache()
                        return gizmo_id
                logger.warning(
                    "Gizmo registration returned %s: %s",
                    res.status_code,
                    res.text[:200],
                )
        except Exception as exc:
            logger.warning("Failed to create Custom GPT on OpenAI: %s", exc)

        return None

    async def sync_user_system_messages(
        self,
        system_prompt: str,
        persona_data: dict[str, Any],
        task_contract: dict[str, Any],
        language: str = "fr-FR",
    ) -> bool:
        """Synchronize persona and task instructions into ChatGPT's user_system_messages."""
        identity = persona_data.get("identity", {})
        name = identity.get("name", "Adam")
        org = identity.get("organization", "OXzoon IPTV")
        is_fr = language.lower().startswith("fr")

        about_user = (
            "Appel sortant de prospection commerciale IPTV. Le prospect décroche son téléphone."
            if is_fr
            else "Outbound sales prospecting call. The customer just answered the phone."
        )

        knowledge = task_contract.get("knowledge", {})
        pricing_lines = []
        for k, v in knowledge.items():
            if "price" in k or "plan" in k or "discount" in k:
                pricing_lines.append(f"{k}: {v}")
        pricing_str = (
            " | ".join(pricing_lines)
            if pricing_lines
            else "Essentiel 10€, Famille 15€, Premium 20€"
        )

        if is_fr:
            about_model = (
                f"Tu es {name}, Directeur Commercial chez {org}, le meilleur vendeur au monde : "
                "chaleureux, dynamique, ultra persuasif et proactif. "
                "CONTEXTE CRUCIAL : C'est TOI qui passes cet appel sortant au client. "
                "Le client n'a pas appelé. Ne dis jamais 'en quoi puis-je vous aider'. "
                "Dès qu'il décroche et dit Allô : salue avec énergie, présente-toi, "
                "annonce l'objet de ton appel (nos abonnements IPTV 4K sport et films) "
                "et pose une question d'accroche. "
                f"Tarifs : {pricing_str}. "
                "Qualifie ses écrans, pitche l'offre, réponds aux objections avec assurance "
                "et propose un test immédiat de 24h ou l'activation. "
                "Reste concis (1 à 2 phrases max par tour) et mène toujours l'échange."
            )
        else:
            about_model = (
                f"You are {name}, Sales Director at {org}, the world's best sales closer: "
                "warm, charismatic, highly persuasive, and proactive. "
                "CRITICAL CONTEXT: YOU are making this outbound prospecting call to the customer. "
                "The customer did not call you. Never say 'how can I help you'. "
                "When they answer: greet energetically, introduce yourself, state reason "
                "(our ultra-HD 4K IPTV service for live sports), and ask a hook question. "
                f"Pricing: {pricing_str}. "
                "Qualify their screens, pitch the ideal plan, handle objections confidently, "
                "and close with an instant 24h trial or activation. "
                "Keep responses punchy (1-2 sentences max) and always lead the conversation."
            )

        try:
            token = await asyncio.to_thread(self.auth_manager.get_token)
        except Exception as exc:
            logger.warning("Could not get auth token for user_system_messages sync: %s", exc)
            return False

        url = "https://chatgpt.com/backend-api/user_system_messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
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

        try:
            async with AsyncSession(impersonate="safari17_0") as session:
                res = await session.post(url, headers=headers, json=payload, timeout=15)
                if res.status_code == 200:
                    logger.info("Synchronized native S2S persona: %s (%s)", name, org)
                    return True
                logger.warning("user_system_messages returned %s: %s", res.status_code, res.text)
        except Exception as exc:
            logger.warning("Failed to sync user_system_messages: %s", exc)

        return False
