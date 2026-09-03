"""ChatGPT Authentication & Persistent OAuth Token Manager for PhoneAgent.

Integrates with OpenAI OAuth 2.0 PKCE Refresh Tokens (e.g. from ~/.codex/auth.json
or ~/.config/phone-agent/chatgpt_session.json) to provide automatic background
token renewal via Safari TLS fingerprint impersonation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from curl_cffi.requests import Session

logger = logging.getLogger("ChatGPTRealtimeAuth")

OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
DEFAULT_SESSION_CACHE = Path.home() / ".config" / "phone-agent" / "chatgpt_session.json"
LEGACY_LOCAL_CACHE = Path(".session_cache.json")


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode JWT payload without signature verification."""
    try:
        parts = token.split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
            return json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode("utf-8"))
    except Exception:
        pass
    return {}


class ChatGPTAuthManager:
    """Manages ChatGPT / OpenAI access token with automatic headless renewal."""

    def __init__(
        self,
        cache_file: Path | None = None,
        codex_auth_path: Path | None = None,
    ) -> None:
        self.cache_file = cache_file or DEFAULT_SESSION_CACHE
        self.codex_auth_path = codex_auth_path or CODEX_AUTH_PATH
        self._lock = threading.RLock()
        self._token: str | None = os.getenv("CHATGPT_ACCESS_TOKEN", "").strip() or None
        self._refresh_token: str | None = None
        self._client_id: str = "app_EMoamEEZ73f0CkXaXp7hrann"
        self._user_info: dict[str, Any] = {}
        self._expires_at: float = 0.0
        self._load_all_sources()

    def _load_all_sources(self) -> None:
        with self._lock:
            # 1. Try to load from ~/.codex/auth.json (Permanent OAuth Refresh Token)
            if self.codex_auth_path.is_file():
                try:
                    with self.codex_auth_path.open("r", encoding="utf-8") as stream:
                        codex_data = json.load(stream)
                    tokens = codex_data.get("tokens", {})
                    if tokens.get("access_token"):
                        self._token = str(tokens["access_token"]).strip()
                    if tokens.get("refresh_token"):
                        self._refresh_token = str(tokens["refresh_token"]).strip()

                    jwt_data = decode_jwt_payload(self._token or "")
                    if jwt_data.get("client_id"):
                        self._client_id = str(jwt_data["client_id"]).strip()
                    self._expires_at = float(jwt_data.get("exp", 0))

                    profile = jwt_data.get("https://api.openai.com/profile", {})
                    auth_meta = jwt_data.get("https://api.openai.com/auth", {})
                    self._user_info = {
                        "name": profile.get("email", "").split("@")[0],
                        "email": profile.get("email", ""),
                        "plan": auth_meta.get("chatgpt_plan_type", "free"),
                        "account_id": auth_meta.get("chatgpt_account_id", ""),
                    }
                except Exception as exc:
                    logger.debug("Could not parse codex auth file: %s", exc)

            # 2. Try to load from user cache file if access token not yet set
            cache_candidates = [self.cache_file, LEGACY_LOCAL_CACHE]
            for candidate in cache_candidates:
                if not self._token and candidate.is_file():
                    try:
                        with candidate.open("r", encoding="utf-8") as stream:
                            cache = json.load(stream)
                        self._token = (
                            cache.get("accessToken") or cache.get("access_token") or None
                        )
                        if cache.get("refresh_token"):
                            self._refresh_token = str(cache["refresh_token"]).strip()
                        self._user_info = cache.get("user", {})
                        if self._token:
                            jwt_data = decode_jwt_payload(self._token)
                            self._expires_at = float(jwt_data.get("exp", 0))
                            break
                    except Exception as exc:
                        logger.debug("Could not parse session cache %s: %s", candidate, exc)

    def is_token_expired(self) -> bool:
        with self._lock:
            if not self._token:
                return True
            if self._expires_at <= 0:
                return False
            # Consider expired if within 60 seconds of expiration
            return time.time() >= (self._expires_at - 60.0)

    def refresh_oauth_token(self) -> str:
        """Use the OAuth refresh_token to request a new access_token from OpenAI.

        Runs headless in Python using Safari TLS impersonation via curl_cffi.
        """
        with self._lock:
            if not self._refresh_token:
                raise RuntimeError(
                    "No OAuth refresh_token available for automatic token renewal. "
                    "Ensure ~/.codex/auth.json or CHATGPT_ACCESS_TOKEN is present."
                )

            data = {
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "refresh_token": self._refresh_token,
            }
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
                ),
            }

            try:
                with Session(impersonate="safari17_0") as session:
                    res = session.post(OAUTH_TOKEN_URL, data=data, headers=headers, timeout=15)
                    if res.status_code != 200:
                        raise RuntimeError(
                            f"OAuth refresh failed ({res.status_code}): {res.text[:200]}"
                        )

                    resp_data = res.json()
                    self._token = str(resp_data["access_token"]).strip()
                    if resp_data.get("refresh_token"):
                        self._refresh_token = str(resp_data["refresh_token"]).strip()

                    jwt_data = decode_jwt_payload(self._token)
                    self._expires_at = float(jwt_data.get("exp", 0))

                    # Persist to session cache file
                    try:
                        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                        with self.cache_file.open("w", encoding="utf-8") as stream:
                            json.dump(
                                {
                                    "accessToken": self._token,
                                    "refresh_token": self._refresh_token,
                                    "user": self._user_info,
                                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                },
                                stream,
                                indent=2,
                            )
                    except Exception as exc:
                        logger.warning("Could not write session cache: %s", exc)

                    # Also update ~/.codex/auth.json if writable
                    if self.codex_auth_path.is_file():
                        try:
                            with self.codex_auth_path.open("r", encoding="utf-8") as stream:
                                codex_data = json.load(stream)
                            if "tokens" in codex_data:
                                codex_data["tokens"]["access_token"] = self._token
                                if self._refresh_token:
                                    codex_data["tokens"]["refresh_token"] = self._refresh_token
                                with self.codex_auth_path.open("w", encoding="utf-8") as stream:
                                    json.dump(codex_data, stream, indent=2)
                        except Exception as exc:
                            logger.debug("Could not update codex auth: %s", exc)

                    logger.info("Successfully refreshed ChatGPT Realtime OAuth access token")
                    return self._token
            except Exception as exc:
                logger.error("Failed to refresh ChatGPT Realtime OAuth token: %s", exc)
                raise

    def get_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid access token, refreshing if needed."""
        with self._lock:
            if force_refresh or self.is_token_expired():
                if self._refresh_token:
                    return self.refresh_oauth_token()
                if self._token:
                    logger.warning("Token may be expired and no refresh_token is available")
                    return self._token
                raise RuntimeError(
                    "No ChatGPT access token available. Please set CHATGPT_ACCESS_TOKEN or "
                    "sign into ChatGPT / Codex."
                )
            if not self._token:
                raise RuntimeError(
                    "No ChatGPT access token available. Please set CHATGPT_ACCESS_TOKEN or "
                    "sign into ChatGPT / Codex."
                )
            return self._token

    @property
    def user_info(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._user_info)
