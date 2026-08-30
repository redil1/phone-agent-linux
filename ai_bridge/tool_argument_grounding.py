"""Preserve caller-dictated text before live tools create durable data.

The Realtime model still owns intent and tool selection. This module only
grounds explicitly dictated literals after the model has selected a tool.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

_TITLE_MARKER = (
    r"(?:named|titled|called|with\s+(?:the\s+)?title|"
    r"nomm[ée]e?|intitul[ée]e?|appel[ée]e?|avec\s+(?:le\s+)?titre|بعنوان)"
)
_DESCRIPTION_MARKER = (
    r"(?:saying|say\s+into|that\s+says|with\s+(?:the\s+)?(?:note|description)|"
    r"disant|qui\s+dit|avec\s+(?:la\s+)?(?:note|description)|"
    r"ويقول|يقول|مع\s+الوصف)"
)
_MESSAGE_MARKER = (
    r"(?:saying|that\s+says|with\s+(?:the\s+)?(?:message|text)|message\s*:|"
    r"disant|qui\s+dit|avec\s+(?:le\s+)?(?:message|texte)|message\s*:|"
    r"ويقول|يقول|بالرسالة|نصها)"
)

_TITLE_RE = re.compile(
    rf"\b{_TITLE_MARKER}\s+(?P<literal>.+?)(?=\s+{_DESCRIPTION_MARKER}\s+|$)",
    re.IGNORECASE | re.UNICODE,
)
_DESCRIPTION_RE = re.compile(
    rf"\b{_DESCRIPTION_MARKER}\s+(?P<literal>.+)$",
    re.IGNORECASE | re.UNICODE,
)
_MESSAGE_RE = re.compile(
    rf"\b{_MESSAGE_MARKER}\s+(?P<literal>.+)$",
    re.IGNORECASE | re.UNICODE,
)
_SUPPORT_ACTION_RE = re.compile(
    r"\b(?:create|open|make|add|cr[ée]e?r?|ouvre|ouvrir)\b.{0,80}\b(?:support\s+)?ticket\b",
    re.IGNORECASE | re.UNICODE,
)
_COMPOUND_MESSAGE_ACTION_RE = re.compile(
    r"\s+(?:and|then|et|puis)\s+(?:also\s+)?(?:send|include|add|envoie|inclure|ajoute)\b",
    re.IGNORECASE | re.UNICODE,
)


@dataclass(frozen=True)
class GroundedToolArguments:
    raw_arguments: str
    grounded_fields: tuple[str, ...] = ()
    blocked: bool = False

    def blocked_output(self) -> str:
        return json.dumps(
            {
                "verified": False,
                "executed": False,
                "error": "literal_confirmation_required",
                "message": (
                    "The caller's dictated text was not clear enough to write safely. "
                    "Ask the caller to repeat the exact wording, then call the tool again."
                ),
                "fields": list(self.grounded_fields),
            },
            ensure_ascii=False,
        )


def _clean_literal(value: str) -> str:
    literal = " ".join(value.strip().split())
    for left, right in (
        ("\"", "\""),
        ("'", "'"),
        ("\u201c", "\u201d"),
        ("\u2018", "\u2019"),
    ):
        if literal.startswith(left) and literal.endswith(right) and len(literal) > 1:
            return literal[len(left) : -len(right)].strip()
    return literal


def _match(pattern: re.Pattern[str], caller_text: str) -> str:
    match = pattern.search(caller_text)
    return _clean_literal(match.group("literal")) if match else ""


def _history(
    caller_text: str,
    transcript_trusted: bool,
    caller_turns: Sequence[tuple[str, bool]] | None,
) -> list[tuple[str, bool]]:
    if caller_turns:
        history = [(str(text).strip(), bool(trusted)) for text, trusted in caller_turns]
        return [(text, trusted) for text, trusted in history if text]
    return [(caller_text.strip(), transcript_trusted)] if caller_text.strip() else []


def _support_window(history: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    for index in range(len(history) - 1, -1, -1):
        if _SUPPORT_ACTION_RE.search(history[index][0]):
            return history[index:]
    return history[-1:]


def _literal_bindings(
    tool_name: str,
    history: list[tuple[str, bool]],
) -> dict[str, tuple[str, bool]]:
    if tool_name == "business_create_support_ticket":
        bindings: dict[str, tuple[str, bool]] = {}
        for text, trusted in _support_window(history):
            subject = _match(_TITLE_RE, text).rstrip(".?! ")
            description = _match(_DESCRIPTION_RE, text)
            if subject:
                bindings["subject"] = (subject, trusted)
            if description:
                bindings["description"] = (description, trusted)
        return bindings
    if tool_name in {
        "whatsapp_send_text_current_customer",
        "whatsapp_reply_current_customer",
    }:
        caller_turn, trusted = history[-1] if history else ("", True)
        text = _match(_MESSAGE_RE, caller_turn)
        # A phrase such as "saying X and also send the ticket number" is a
        # compound request, not one literal message. Let the model compose it;
        # replacing the whole argument with the command sentence would be worse.
        if text and not _COMPOUND_MESSAGE_ACTION_RE.search(text):
            return {"text": (text, trusted)}
        return {}
    if tool_name == "business_update_support_ticket":
        caller_turn, trusted = history[-1] if history else ("", True)
        comment = _match(_MESSAGE_RE, caller_turn)
        return {"comment": (comment, trusted)} if comment else {}
    return {}


def ground_tool_arguments(
    tool_name: str,
    raw_arguments: str,
    caller_text: str,
    *,
    transcript_trusted: bool = True,
    caller_turns: Sequence[tuple[str, bool]] | None = None,
) -> GroundedToolArguments:
    """Ground explicit literals, or block a mismatch on uncertain audio."""

    if not caller_text.strip():
        return GroundedToolArguments(raw_arguments=raw_arguments)
    try:
        arguments = json.loads(raw_arguments) if raw_arguments else {}
    except json.JSONDecodeError:
        return GroundedToolArguments(raw_arguments=raw_arguments)
    if not isinstance(arguments, dict):
        return GroundedToolArguments(raw_arguments=raw_arguments)
    history = _history(caller_text, transcript_trusted, caller_turns)
    bindings = _literal_bindings(tool_name, history)
    if not bindings:
        return GroundedToolArguments(raw_arguments=raw_arguments)

    changed = tuple(
        field for field, (literal, _trusted) in bindings.items() if arguments.get(field) != literal
    )
    if not changed:
        return GroundedToolArguments(raw_arguments=raw_arguments)
    if any(not bindings[field][1] for field in changed):
        return GroundedToolArguments(
            raw_arguments=raw_arguments,
            grounded_fields=changed,
            blocked=True,
        )
    for field in changed:
        arguments[field] = bindings[field][0]
    return GroundedToolArguments(
        raw_arguments=json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
        grounded_fields=changed,
    )
