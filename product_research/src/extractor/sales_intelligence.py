"""How this product is sold, learned from its own site and from its market.

The product extractor answers *what is true*. This answers *how to sell it*:
which objections this market actually raises, what language the buyer already
responds to, and how a person who works here would say things out loud.

Two sources, deliberately kept apart:

* **The product's own site** — its FAQ, support and terms pages are a written
  record of the objections real customers raise, in their own words. Answers
  drawn from here may be spoken, because they are the company's own claims.
* **The wider market** — competitor and category pages shape *positioning*:
  what buyers compare against, what they worry about, which angles land. None
  of it is ever quoted. Repeating a competitor's marketing, or a price that
  changed last week, to a live customer is a claim the company cannot stand
  behind.

That split is the whole design. Market knowledge changes how the agent sells;
only the company's own site changes what it may assert.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("SalesIntelligence")

# Enough of the site to cover FAQ and support pages without burying the model.
OWN_SITE_BUDGET_CHARS = 40_000
MARKET_BUDGET_CHARS = 20_000
MAX_OBJECTIONS = 12
MAX_PHRASES = 12

SYSTEM_PROMPT = """You are a senior sales strategist preparing a phone agent.

You are given a company's own website, and separately some market context.

You also know this product category well from your own experience of it. Use
that knowledge for positioning: what buyers in this category compare, what they
worry about, which angles land. That knowledge shapes HOW to sell. It never
becomes a fact the agent states.

Return JSON only, matching this shape exactly:

{
  "buyer": {
    "who": "one sentence on who actually buys this",
    "trigger": "what makes them start looking",
    "decides_on": ["the two or three things that actually decide the sale"]
  },
  "vocabulary": ["6-10 words or short phrases the site itself uses to describe
                  the value; the buyer already responds to these"],
  "objections": [
    {"objection": "what the customer says, in their words",
     "answer": "how to answer it in one or two spoken sentences",
     "source": "faq | support | terms | market"}
  ],
  "sample_phrases": {
    "stating_price": "state the real price plainly, no apology, no hedging",
    "meeting_doubt": "answer doubt about the company, not about price",
    "holding_position": "hold the position without pushing",
    "admitting_gap": "admit not knowing something, with certainty",
    "first_discovery": "the first question after permission is granted",
    "opening_hook": "one sentence that earns the next thirty seconds"
  },
  "discovery_questions": ["3-5 questions that qualify for THIS product"]
}

Rules:
- Objections must come from what the site's FAQ and support pages actually
  answer. Do not invent objections nobody raises.
- Answers must be sayable out loud in one or two sentences. No lists, no
  markdown, no numbers you did not see on the company's own site.
- Sample phrases must sound like a person who works there, not a script.
  Short. Spoken. No jargon the site does not use.
- Use market context and your own category knowledge ONLY to choose angle and
  anticipate concerns. Never quote a competitor, never name one, never cite a
  price or claim from anywhere but the company's own site.
- Every number in an answer must appear on the company's own site.
- Return JSON only."""


def build_prompt(
    own_site: str,
    market_context: str,
    product_name: str,
    language: str = "English",
) -> str:
    """Assemble the user prompt, keeping the two sources clearly separated."""

    market_block = (
        f"""
MARKET CONTEXT (for positioning only — never quote, never name a competitor)
=====================================================================
{market_context[:MARKET_BUDGET_CHARS]}
=====================================================================
"""
        if market_context.strip()
        else (
            "\nMARKET CONTEXT: none gathered. Draw on your own knowledge of this "
            "product category for positioning only.\n"
        )
    )
    return f"""PRODUCT: {product_name}
SPOKEN LANGUAGE: {language}

THE COMPANY'S OWN WEBSITE (the only source you may quote facts from)
=====================================================================
{own_site[:OWN_SITE_BUDGET_CHARS]}
=====================================================================
{market_block}
Return the JSON object described in your instructions."""


def _clean_text(value: Any, limit: int = 400) -> str:
    return " ".join(str(value or "").split())[:limit]


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape a model reply into what a task contract accepts.

    Anything malformed is dropped rather than repaired: a half-understood
    objection is worse on a call than no objection at all.
    """

    buyer = raw.get("buyer") if isinstance(raw.get("buyer"), dict) else {}
    objections: list[dict[str, str]] = []
    for entry in raw.get("objections") or []:
        if not isinstance(entry, dict):
            continue
        objection = _clean_text(entry.get("objection"))
        answer = _clean_text(entry.get("answer"))
        if not objection or not answer:
            continue
        item = {"objection": objection, "answer": answer}
        source = _clean_text(entry.get("source"), 40).lower()
        if source in {"faq", "support", "terms", "market"}:
            item["source"] = source
        objections.append(item)
        if len(objections) >= MAX_OBJECTIONS:
            break

    phrases: dict[str, str] = {}
    for key, value in (raw.get("sample_phrases") or {}).items():
        name = _clean_text(key, 40).replace(" ", "_").lower()
        spoken = _clean_text(value)
        if name and spoken:
            phrases[name] = spoken
        if len(phrases) >= MAX_PHRASES:
            break

    return {
        "buyer": {
            "who": _clean_text(buyer.get("who")),
            "trigger": _clean_text(buyer.get("trigger")),
            "decides_on": [_clean_text(x, 120) for x in (buyer.get("decides_on") or [])][:4],
        },
        "vocabulary": [_clean_text(x, 60) for x in (raw.get("vocabulary") or [])][:10],
        "objections": objections,
        "sample_phrases": phrases,
        "discovery_questions": [
            _clean_text(x, 200) for x in (raw.get("discovery_questions") or [])
        ][:5],
    }


async def extract_sales_intelligence(
    llm_client: Any,
    *,
    own_site: str,
    market_context: str,
    product_name: str,
    language: str = "English",
) -> dict[str, Any]:
    """Learn how to sell this product. Never fatal: facts alone still work."""

    try:
        raw = await llm_client.generate_json(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_prompt(own_site, market_context, product_name, language),
        )
    except Exception as exc:
        logger.warning("Sales intelligence pass failed, falling back to defaults: %s", exc)
        return normalize({})
    if not isinstance(raw, dict):
        logger.warning("Sales intelligence returned %s, expected an object", type(raw).__name__)
        return normalize({})
    result = normalize(raw)
    logger.info(
        "Sales intelligence: %d objections, %d phrases, %d discovery questions",
        len(result["objections"]),
        len(result["sample_phrases"]),
        len(result["discovery_questions"]),
    )
    return result


def as_json(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)
