"""Deterministic Permission & Safety Gates for PhoneAgent.

Enforces non-negotiable behavior in pure Python code with mathematical certainty.
"""

from __future__ import annotations

import re


class PermissionGate:
    """Enforces non-negotiable boundaries, financial limits, and output sanitization."""

    PROHIBITED_PHRASES = (
        r"\b(as an ai language model|as an ai)\b",
        r"\b(en tant que modèle de langage|en tant qu'ia)\b",
    )
    UNVERIFIED_ACTION_CLAIMS = (
        r"\b(i|we)\s+(booked|reserved|paid|refunded|sent|deleted|updated)\b",
        r"\b(j'ai|nous avons)\s+(réservé|payé|remboursé|envoyé|supprimé|modifié)\b",
    )

    @classmethod
    def sanitize_for_telephony(cls, text: str) -> str:
        """Strip markdown, symbols, asterisks, and emojis to produce pure telephony speech."""
        if not text:
            return ""

        # Remove code blocks and markdown headers
        cleaned = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        cleaned = re.sub(r"[#*`_~>\-•]", " ", cleaned)

        # Remove emojis
        emoji_pattern = re.compile(
            "["
            "\U0001f600-\U0001f64f"  # emoticons
            "\U0001f300-\U0001f5ff"  # symbols & pictographs
            "\U0001f680-\U0001f6ff"  # transport & map
            "\U0001f1e0-\U0001f1ff"  # flags
            "\U00002702-\U000027b0"
            "\U000024c2-\U0001f251"
            "]+",
            flags=re.UNICODE,
        )
        cleaned = emoji_pattern.sub("", cleaned)

        # Normalize whitespace
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    @classmethod
    def check_compliance(cls, text: str) -> tuple[bool, list[str]]:
        """Verify output does not contain prohibited robotic expressions."""
        violations: list[str] = []
        for pattern in cls.PROHIBITED_PHRASES:
            if re.search(pattern, text, re.IGNORECASE):
                violations.append(f"Contains prohibited robotic phrase matching {pattern}")
        return len(violations) == 0, violations

    @classmethod
    def verify_financial_limit(cls, amount: float, max_limit: float = 0.0) -> bool:
        """Reject financial commitment if above authorized zero-trust threshold."""
        return 0.0 <= amount <= max_limit

    @classmethod
    def enforce_spoken_response(
        cls,
        text: str,
        *,
        language: str,
        verified_actions: set[str] | None = None,
    ) -> tuple[str, list[str]]:
        """Sanitize speech and prevent unsupported external-action claims."""

        cleaned = cls.sanitize_for_telephony(text)
        compliant, violations = cls.check_compliance(cleaned)
        if not compliant:
            cleaned = cls._safe_fallback(language)
        if re.search(r"[\u0600-\u06ff]", cleaned):
            violations.append("Response used a language outside the English/French policy")
            cleaned = cls._safe_fallback(language)
        if not verified_actions:
            for pattern in cls.UNVERIFIED_ACTION_CLAIMS:
                if re.search(pattern, cleaned, re.IGNORECASE):
                    violations.append("Claimed an external action without a verified tool result")
                    cleaned = cls._safe_fallback(language)
                    break
        return cleaned, violations

    @staticmethod
    def _safe_fallback(language: str) -> str:
        if language.lower().startswith("fr"):
            return (
                "Je ne peux pas confirmer cette action sans vérification. "
                "Je peux recueillir les informations nécessaires."
            )
        if language.lower().startswith("en"):
            return (
                "I cannot confirm that action without verification. "
                "I can collect the required information."
            )
        return (
            "I cannot confirm that action without verification. "
            "I can collect the required information."
        )
