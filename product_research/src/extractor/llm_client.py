"""
Unified Multi-Provider LLM Client.
Supports Ollama (Local), OpenAI, Google Gemini, Anthropic, and Offline Deterministic Fallback.
"""

import json
import os
import re
from typing import Any

import httpx

# A full seven-pillar extraction is a single very large completion.
EXTRACTION_TIMEOUT_SECS = float(os.getenv("PRODUCT_EXTRACTION_TIMEOUT_SECS", "600"))


class LLMClient:
    def __init__(
        self,
        provider: str = "auto",
        model: str | None = None,
        api_key: str | None = None,
        ollama_base_url: str = "http://localhost:11434",
        custom_base_url: str | None = None
    ):
        self.provider = provider.lower()
        self.model = model
        self.api_key = api_key
        self.ollama_base_url = os.getenv("OLLAMA_BASE_URL", ollama_base_url)
        self.custom_base_url = custom_base_url or os.getenv("OPENAI_BASE_URL")

        # Auto-detect provider if set to auto
        if self.provider == "auto":
            if os.getenv("OPENAI_API_KEY"):
                self.provider = "openai"
                self.model = self.model or "gpt-4o"
                self.api_key = os.getenv("OPENAI_API_KEY")
            elif os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
                self.provider = "gemini"
                self.model = self.model or "gemini-2.5-flash"
                self.api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            elif os.getenv("ANTHROPIC_API_KEY"):
                self.provider = "anthropic"
                self.model = self.model or "claude-3-5-sonnet-20241022"
                self.api_key = os.getenv("ANTHROPIC_API_KEY")
            else:
                # A signed-in subscription beats a small local model, and costs
                # nothing extra: prefer Codex, then Antigravity, then Ollama.
                try:
                    from .subscription_providers import available_subscription_providers
                    subscriptions = available_subscription_providers()
                except Exception:
                    subscriptions = {}
                for name in ("codex", "antigravity"):
                    if name in subscriptions:
                        self.provider = name
                        self.model = self.model or subscriptions[name][0]
                        break
                if self.provider != "auto":
                    return
                # Default to ollama, choosing a model that is actually pulled.
                # Hardcoding one name failed with a bare 404 on every machine
                # that did not happen to have it.
                self.provider = "ollama"
                self.model = self.model or self._first_installed_ollama_model()

    def _installed_ollama_models(self) -> list:
        """Names of the models this Ollama instance can actually serve."""
        try:
            import httpx as _httpx
            res = _httpx.get(f"{self.ollama_base_url}/api/tags", timeout=5.0)
            if res.status_code != 200:
                return []
            return [m.get("name", "") for m in res.json().get("models", []) if m.get("name")]
        except Exception:
            return []

    def _first_installed_ollama_model(self) -> str:
        """Prefer a locally pulled model, then a cloud one, then give up."""
        models = self._installed_ollama_models()
        local = [name for name in models if not name.endswith(":cloud")]
        return (local or models or ["llama3.3"])[0]

    async def generate_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Calls the configured LLM provider and returns parsed JSON."""
        if self.provider == "ollama":
            return await self._call_ollama(system_prompt, user_prompt)
        elif self.provider in ("openai", "custom_openai", "kimi", "moonshot"):
            return await self._call_openai(system_prompt, user_prompt)
        elif self.provider == "gemini":
            return await self._call_gemini(system_prompt, user_prompt)
        elif self.provider == "anthropic":
            return await self._call_anthropic(system_prompt, user_prompt)
        elif self.provider == "codex":
            from .subscription_providers import call_codex
            return await call_codex(system_prompt, user_prompt, self.model)
        elif self.provider == "antigravity":
            from .subscription_providers import call_antigravity
            return await call_antigravity(system_prompt, user_prompt, self.model)
        else:
            raise ValueError(f"Unsupported LLM provider: {self.provider}")


    async def _call_ollama(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Calls local Ollama instance with JSON format constraint."""
        url = f"{self.ollama_base_url}/api/chat"
        payload = {
            "model": self.model or self._first_installed_ollama_model(),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1}
        }
        try:
            # A seven-pillar extraction feeds ~45k characters in. Two minutes is
            # not enough for a large or cloud-hosted model, and the timeout
            # surfaced as an empty error because httpx timeouts stringify to "".
            async with httpx.AsyncClient(timeout=EXTRACTION_TIMEOUT_SECS) as client:
                res = await client.post(url, json=payload)
                if res.status_code == 200:
                    data = res.json()
                    content = data.get("message", {}).get("content", "{}")
                    return self._clean_and_parse_json(content)
                elif res.status_code == 404:
                    available = self._installed_ollama_models()
                    raise RuntimeError(
                        f"Ollama has no model {payload['model']!r}. "
                        + (
                            "Installed: " + ", ".join(available)
                            if available
                            else f"No models are installed; run: ollama pull {payload['model']}"
                        )
                    )
                else:
                    raise RuntimeError(f"Ollama returned HTTP {res.status_code}: {res.text}")
        except httpx.TimeoutException as e:
            raise RuntimeError(
                f"Ollama did not answer within {EXTRACTION_TIMEOUT_SECS:.0f}s for model "
                f"{payload['model']!r} ({type(e).__name__}). Try a faster model, or lower "
                f"--max-pages so there is less text to extract from."
            ) from e
        except Exception as e:
            # Include the type: several httpx errors have an empty str().
            raise RuntimeError(
                f"Failed to communicate with Ollama at {url}: {type(e).__name__}: {e}"
            ) from e

    async def _call_openai(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Calls OpenAI or OpenAI-compatible (Kimi/Moonshot, DeepSeek, vLLM) Chat Completions API."""
        api_key = self.api_key or os.getenv("OPENAI_API_KEY") or os.getenv("MOONSHOT_API_KEY") or "dummy-key"

        base_url = (self.custom_base_url or "https://api.openai.com/v1").rstrip("/")
        url = f"{base_url}/chat/completions"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model or "gpt-4o",
            "messages": [
                {"role": "system", "content": system_prompt + "\nCRITICAL: Return valid JSON ONLY. No markdown formatting."},
                {"role": "user", "content": user_prompt}
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            res = await client.post(url, json=payload, headers=headers)
            if res.status_code == 200:
                data = res.json()
                content = data["choices"][0]["message"]["content"]
                return self._clean_and_parse_json(content)
            else:
                raise RuntimeError(f"OpenAI/Compatible API Error ({res.status_code}): {res.text}")


    async def _call_gemini(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Calls Google Gemini API."""
        api_key = self.api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not configured.")

        model_name = self.model or "gemini-2.5-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": f"System Instructions:\n{system_prompt}\n\nUser Content:\n{user_prompt}"}
                    ]
                }
            ],
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": 0.1
            }
        }
        async with httpx.AsyncClient(timeout=90.0) as client:
            res = await client.post(url, json=payload)
            if res.status_code == 200:
                data = res.json()
                candidates = data.get("candidates", [])
                if candidates:
                    content = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
                    return self._clean_and_parse_json(content)
                return {}
            else:
                raise RuntimeError(f"Gemini API Error ({res.status_code}): {res.text}")

    async def _call_anthropic(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Calls Anthropic Claude Messages API."""
        api_key = self.api_key or os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not configured.")

        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        payload = {
            "model": self.model or "claude-3-5-sonnet-20241022",
            "max_tokens": 8192,
            "system": system_prompt + "\nIMPORTANT: Return valid JSON ONLY. No markdown fences.",
            "messages": [
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.1
        }
        async with httpx.AsyncClient(timeout=90.0) as client:
            res = await client.post(url, json=payload, headers=headers)
            if res.status_code == 200:
                data = res.json()
                content = data["content"][0]["text"]
                return self._clean_and_parse_json(content)
            else:
                raise RuntimeError(f"Anthropic API Error ({res.status_code}): {res.text}")

    def _clean_and_parse_json(self, raw_str: str) -> dict[str, Any]:
        """Strips markdown code fences and parses JSON safely."""
        text = raw_str.strip()
        # Remove ```json ... ``` or ``` ... ```
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\n?", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\n?```$", "", text)
            text = text.strip()
        
        # If there's surrounding text, extract the outermost JSON object
        start_idx = text.find("{")
        end_idx = text.rfind("}")
        if start_idx != -1 and end_idx != -1:
            text = text[start_idx:end_idx + 1]

        return json.loads(text)
