"""Turn a crawled product knowledge base into a live PhoneAgent task contract.

The agent states these facts to real customers as verified truth, so nothing is
taken on the extractor's word. Every checkable claim - each price, percentage,
duration, certification and product name - must be found in the crawled source
*near its own subject* before it is allowed into the contract. A claim that
cannot be traced is dropped rather than softened.

That is what makes automatic activation defensible. The failure mode is a
missing fact, never a wrong one: with the fact absent, `knowledge_base_search`
returns ``found: false`` and the agent says it does not have that detail
instead of inventing one.

Auto-apply is refused outright when something is wrong that dropping a fact
cannot fix - an unverifiable price, a tool the contract promises but nothing
implements, or a contract the runtime will not accept.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .task_engine import TaskEngine

logger = logging.getLogger("ProductImport")

# How far from a claim's subject the supporting text may sit in the source.
# Wide enough for a pricing card's markup, tight enough that a number lifted
# from an unrelated page does not count as proof.
GROUNDING_WINDOW_CHARS = 400
# Contracts cap knowledge at 40 entries of 400 characters.
MAX_FACTS = 40
MAX_FACT_CHARS = 400
# Below this the crawl found too little to sell from, whatever it claims.
MIN_ACCEPTED_FACTS = 6
# A price or compliance claim that will not verify is the strongest signal that
# an extraction went wrong. It no longer stops activation by default, because
# the claim is dropped either way and never reaches the caller: the agent simply
# says it does not have that detail. Set strict=True to require a human look
# before a contract with an unverifiable price goes live.
CRITICAL_TOPICS = ("pricing", "security", "compliance", "trial", "refund", "discount")

_ACRONYM = re.compile(r"\b[A-Z][A-Z0-9]{2,}\b")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_WORD = re.compile(r"[\wÀ-ɏ]+")
_CURRENCY_WORDS = frozenset(
    {"$", "€", "£", "usd", "eur", "dollar", "dollars", "euro", "euros"}
)
# Sales pages write "twelve months" as often as "12 months", and a contract may
# spell prices out for the voice. Both must count as the same claim.
_NUMBER_WORDS: dict[str, str] = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "fifteen": "15", "twenty": "20",
    "twenty-five": "25", "thirty": "30", "thirty-nine": "39", "forty": "40",
    "fifty": "50", "fifty-nine": "59", "sixty": "60", "ninety": "90",
    "hundred": "100", "thousand": "1000",
}
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "you", "your", "our", "are", "all", "not",
        "per", "any", "can", "has", "have", "from", "that", "this", "into",
        "plan", "plans", "available", "included", "includes", "custom", "based",
    }
)


@dataclass(frozen=True, slots=True)
class FactCheck:
    """One candidate fact and whether the source actually supports it."""

    topic: str
    fact: str
    grounded: bool
    reason: str = ""

    @property
    def is_critical(self) -> bool:
        return any(word in self.topic.lower() for word in CRITICAL_TOPICS)


@dataclass(frozen=True, slots=True)
class ImportReport:
    """Everything a decision to activate this contract should rest on."""

    task_id: str
    product_name: str
    contract: dict[str, Any]
    accepted: tuple[FactCheck, ...] = ()
    rejected: tuple[FactCheck, ...] = ()
    blocking: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def can_auto_apply(self) -> bool:
        return not self.blocking

    def summary(self) -> str:
        verdict = "READY" if self.can_auto_apply else "BLOCKED"
        return (
            f"{verdict}: {self.product_name} -> {self.task_id} "
            f"({len(self.accepted)} verified, {len(self.rejected)} dropped, "
            f"{len(self.blocking)} blocking, {len(self.warnings)} warnings)"
        )


# --------------------------------------------------------------------------
# Grounding
# --------------------------------------------------------------------------


def _numeric_claims(text: str) -> list[tuple[str, re.Pattern[str]]]:
    """Every number in a claim, paired with a pattern that proves it.

    A bare digit run is far too weak to confirm anything. Searching for "15" to
    support a fifteen percent discount matched "May 15, 2025" on a promotions
    page, which would have let the agent offer a discount nobody authorised. A
    number is therefore only ever verified together with the unit or currency
    that gives it meaning.
    """

    claims: list[tuple[str, re.Pattern[str]]] = []
    for match in re.finditer(
        r"(?<![A-Za-z0-9])(?P<currency>[$€£]\s*)?(?P<number>\d+(?:[.,]\d+)?)\s*(?P<unit>%|percent|"
        r"[$€£]|usd|eur|dollars?|euros?|/\s*\w+|min\b|minutes?|months?|days?|hours?|"
        r"weeks?|years?|seats?|screens?|msg\b)?",
        text,
        re.IGNORECASE,
    ):
        number = match.group("number")
        # A model normalises "$ 25" on the page to "$25.00". Demanding the
        # trailing zeros rejected every genuine price on a site that omits them,
        # which blocks activation on facts that are perfectly well grounded.
        forms = {number}
        parts = re.split(r"[.,]", number, maxsplit=1)
        whole = parts[0]
        fraction = parts[1] if len(parts) > 1 else ""
        if fraction and set(fraction) == {"0"}:
            forms.add(whole)
        elif not fraction:
            forms.update({f"{whole}.00", f"{whole},00"})
        alternatives = [
            re.escape(form).replace(r"\,", "[.,]").replace(r"\.", "[.,]")
            for form in sorted(forms, key=len, reverse=True)
        ]
        alternatives += [
            re.escape(word) for word, value in _NUMBER_WORDS.items() if value == number
        ]
        loose = "(?:" + "|".join(alternatives) + ")"
        unit = (match.group("unit") or "").strip().lower()
        currency = (match.group("currency") or "").strip()
        if unit in ("%", "percent"):
            pattern = rf"{loose}\s*(?:%|percent)"
        elif currency or unit in _CURRENCY_WORDS:
            symbol = re.escape(currency or unit) if (currency or unit) in "$€£" else "[$€£]"
            pattern = rf"(?:{symbol}\s*{loose}|{loose}\s*(?:dollars?|euros?|usd|eur))"
        elif unit:
            pattern = rf"{loose}\s*\+?\s*{re.escape(unit).replace(chr(92) + 's', chr(92) + 's')}"
        else:
            # No unit: fall back to the bare number, still subject to the
            # proximity check below.
            pattern = rf"\b{loose}\b"
        claims.append((f"{currency}{number}{(' ' + unit) if unit else ''}".strip(),
                       re.compile(pattern, re.IGNORECASE)))
    return claims


def _acronyms(text: str) -> list[str]:
    return [token for token in _ACRONYM.findall(text) if len(token) <= 12]


def _subject_terms(topic: str, fact: str) -> list[str]:
    """Distinctive words that should sit beside the claim in the source.

    Drawn from the topic and from proper nouns only, never from the claim's
    ordinary prose. Taking terms from the whole sentence let a fabricated fact
    supply its own corroboration: the invented wording would be searched for,
    found nowhere, and any loose word near the number would vouch for it.
    """

    proper_nouns = [
        word for word in re.findall(r"\b[A-Z][\w-]{2,}\b", fact) if word.lower() not in _STOPWORDS
    ]
    words = [
        word.lower()
        for word in _WORD.findall(topic.replace("_", " ") + " " + " ".join(proper_nouns))
        if len(word) > 3 and word.lower() not in _STOPWORDS and not word.isdigit()
    ]
    seen: list[str] = []
    for word in words:
        if word not in seen:
            seen.append(word)
    return seen[:8]


def verify_fact(
    topic: str, fact: str, source: str, *, require_proximity: bool = True
) -> FactCheck:
    """Confirm a claim against the crawled source, or reject it.

    ``require_proximity`` binds a number to its subject, which is what keeps a
    plan's price attached to that plan. Turn it off for free-standing spoken
    lines such as objection answers, where the topic is the customer's own
    wording and will never appear on the company's site. Those are still held
    to the harder half of the rule: every number must exist in the source with
    its unit, so a competitor's price learned from the market cannot be spoken.
    """

    lowered = source.lower()

    for acronym in _acronyms(fact):
        if acronym.lower() not in lowered:
            return FactCheck(topic, fact, False, f"{acronym!r} does not appear in the source")

    claims = _numeric_claims(fact)
    if not claims:
        return FactCheck(topic, fact, True, "no numeric claim to verify")

    subjects = _subject_terms(topic, fact) if require_proximity else []
    for rendered, pattern in claims:
        matches = list(pattern.finditer(source))
        if not matches:
            return FactCheck(
                topic, fact, False, f"{rendered!r} does not appear in the source"
            )
        if not subjects:
            continue
        near = False
        for match in matches:
            low = max(0, match.start() - GROUNDING_WINDOW_CHARS)
            high = match.end() + GROUNDING_WINDOW_CHARS
            if any(subject in lowered[low:high] for subject in subjects):
                near = True
                break
        if not near:
            return FactCheck(
                topic, fact, False, f"{rendered!r} never appears near this claim's subject"
            )
    return FactCheck(topic, fact, True, "every number and acronym traced to the source")


# --------------------------------------------------------------------------
# Pillar condensation
# --------------------------------------------------------------------------


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_FACT_CHARS else text[: MAX_FACT_CHARS - 1].rstrip() + "…"


def _slug(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    return slug[:limit] or "item"


def candidate_facts(kb: dict[str, Any]) -> dict[str, str]:
    """Condense the seven pillars into contract-sized speakable facts.

    A contract allows forty facts, so this keeps what a salesperson would
    actually say out loud and drops the rest.
    """

    facts: dict[str, str] = {}

    def add(topic: str, value: Any) -> None:
        text = _clip(value)
        if text and topic not in facts and len(facts) < MAX_FACTS:
            facts[topic] = text

    # Where to actually buy. Asked for it, the agent answered "I don't have a
    # website address to give you", because nothing in the pillars carries the
    # company's own address and every spoken fact must come from this block.
    website = str(kb.get("website_url") or "").strip()
    company = str(kb.get("company_name") or kb.get("product_name") or "").strip()
    if website:
        spoken = website.replace("https://", "").replace("http://", "").rstrip("/")
        add("website_address", f"{company or spoken} is online at {spoken}.".strip())

    commercials = kb.get("commercials_pricing") or {}
    for plan in commercials.get("plans") or []:
        if not isinstance(plan, dict):
            continue
        name = plan.get("name") or "plan"
        bits = [
            f"{name}: {plan.get('price_monthly')} per {plan.get('billing_unit', 'month')}".strip(),
            f"annual {plan['price_annual']}" if plan.get("price_annual") else "",
            "includes " + ", ".join(plan.get("includes") or []) if plan.get("includes") else "",
        ]
        add(f"pricing_{_slug(name)}", ". ".join(part for part in bits if part))
    for key in ("trial_policy", "payment_terms", "contract_terms",
                "cancellation_and_refund_policy", "hard_margin_floor"):
        if commercials.get(key):
            add(f"pricing_{_slug(key)}", commercials[key])
    for discount in commercials.get("discount_matrix") or []:
        if isinstance(discount, dict) and discount.get("scenario"):
            add(
                f"discount_{_slug(discount['scenario'])}",
                f"{discount['scenario']}: up to {discount.get('max_discount_pct')}% "
                f"{discount.get('conditions', '')}",
            )

    core = kb.get("core_specs") or {}
    if core.get("summary"):
        add("product_summary", core["summary"])
    for feature in (core.get("features") or [])[:8]:
        if isinstance(feature, dict) and feature.get("name"):
            add(f"feature_{_slug(feature['name'])}",
                f"{feature['name']}: {feature.get('description', '')}")

    value = kb.get("value_prop_roi") or {}
    if value.get("primary_tagline"):
        add("value_tagline", value["primary_tagline"])
    roi = value.get("roi_benchmarks")
    if isinstance(roi, dict):
        for key, text in list(roi.items())[:4]:
            add(f"proof_{_slug(key)}", text)

    for competitor in (kb.get("competitive_intel") or {}).get("battlecards") or []:
        if isinstance(competitor, dict) and competitor.get("competitor_name"):
            add(
                f"competitor_{_slug(competitor['competitor_name'])}",
                f"Versus {competitor['competitor_name']}: "
                + ", ".join(competitor.get("our_advantage") or []),
            )

    support = kb.get("implementation_support") or {}
    for key in ("timeline", "onboarding_process", "support_tiers", "training_materials"):
        if support.get(key):
            add(f"support_{_slug(key)}", support[key])

    security = kb.get("security_compliance") or {}
    if security.get("certifications"):
        add("security_certifications", "Certified: " + ", ".join(security["certifications"]))
    for key in ("uptime_guarantee", "encryption_standards", "data_privacy_policy", "hosting"):
        if security.get(key):
            add(f"security_{_slug(key)}", security[key])

    guardrails = kb.get("guardrails_disqualifiers") or {}
    if guardrails.get("unsupported_features"):
        add("guardrail_not_supported",
            "Not supported: " + ", ".join(guardrails["unsupported_features"]))
    return facts


# --------------------------------------------------------------------------
# Contract construction
# --------------------------------------------------------------------------


def build_contract(
    kb: dict[str, Any],
    verified_facts: dict[str, str],
    *,
    task_id: str,
    agent_name: str,
    allowed_tools: list[str],
    spoken_max_words: int,
    spoken_sentence_limit: int,
    sales_intelligence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a contract from verified facts only."""

    product = str(kb.get("product_name") or "the product").strip()
    company = str(kb.get("company_name") or product).strip()
    tagline = str((kb.get("value_prop_roi") or {}).get("primary_tagline")
                  or kb.get("tagline") or "").strip()
    guardrails = kb.get("guardrails_disqualifiers") or {}
    intelligence = sales_intelligence or {}

    slots = [
        {
            "id": "permission_to_continue",
            "question": "whether now is a good time to talk",
            # Reused verbatim: hand-tuned bilingual patterns beat generated ones.
            "detect": [
                r"\b(oui|d'?accord|allez[- ]y|vas[- ]y|bien s[uû]r|yes|sure|go ahead|okay|ok)\b"
            ],
        }
    ]
    # Questions this product actually qualifies on, learned from its own site,
    # in preference to one generic question per marketing persona.
    for index, question in enumerate(intelligence.get("discovery_questions") or [], start=1):
        text = str(question).strip()
        if text:
            slots.append({"id": f"discovery_{index}", "question": _clip(text)})
    if len(slots) == 1:
        for persona in ((kb.get("value_prop_roi") or {}).get("persona_messaging") or [])[:4]:
            if isinstance(persona, dict) and persona.get("role_title"):
                slots.append(
                    {
                        "id": _slug(f"need_{persona['role_title']}", 40),
                        # No detect pattern: a generated regex that mis-fires
                        # would mark a question answered that the caller never
                        # answered.
                        "question": f"what matters most to them as {persona['role_title']}",
                    }
                )

    return {
        "id": task_id,
        "title": f"{product} sales call",
        "objective": _clip(
            f"Sell {product} by {company} through an honest consultative phone call: "
            f"earn permission, understand the caller's situation, connect only verified "
            f"facts to it, handle concerns truthfully, and ask for one clear next step."
            + (f" Positioning: {tagline}" if tagline else "")
        ),
        "opening_greeting": {
            "en": f"Hello, this is {agent_name} from {company}. "
                  f"I'm calling about {product}. Is now a good time for a quick chat?",
            "fr": f"Bonjour, ici {agent_name} de {company}. "
                  f"Je vous appelle au sujet de {product}. Est-ce un bon moment pour échanger ?",
        },
        # Forced to the tuned voice settings, never the generator's own targets.
        "spoken_max_words": spoken_max_words,
        "spoken_sentence_limit": spoken_sentence_limit,
        "inputs_required": slots,
        "success_criteria": [
            "state_company_and_call_purpose_once",
            "earn_permission_to_continue",
            "discover_at_least_one_real_customer_need",
            "connect_verified_value_to_that_need",
            "handle_concerns_with_empathy_and_accuracy",
            "ask_for_one_clear_low_friction_next_step",
            "respect_a_clear_refusal_immediately",
        ],
        "conversation_strategy": [
            f"OPEN: Introduce {agent_name}, {company} and the purpose once; ask permission.",
            "DISCOVER: Ask one short question at a time and listen for the real need.",
            "QUALIFY: Decide whether there is a genuine fit before pitching.",
            "RECOMMEND: Present one best-fit option using only verified facts.",
            "OBJECTIONS: Answer the objection actually raised, honestly, then check\n"
            "  whether the concern is resolved.",
            "CLOSE: Ask for one concrete next step when interest is present.",
            "RECOVER: Bridge back naturally if the caller goes off topic.",
        ],
        "natural_conversation_rules": [
            "never_repeat_the_opening_after_the_call_has_started",
            "after_permission_is_granted_move_directly_to_discovery",
            "never_ask_a_question_the_caller_already_answered",
            "respect_a_refusal_immediately_and_close_warmly",
        ],
        "ground_truth_policy": [
            "only_state_facts_present_in_the_verified_knowledge_block",
            "when_information_is_missing_say_so_briefly_and_offer_to_confirm_it",
            "never_claim_an_action_completed_without_a_verified_tool_result",
        ],
        "knowledge": verified_facts,
        # How to say it, and which objections this market really raises. These
        # shape delivery only; every fact spoken still comes from `knowledge`.
        **(
            {"sample_phrases": {k: {"en": v} for k, v in intelligence["sample_phrases"].items()}}
            if intelligence.get("sample_phrases")
            else {}
        ),
        **(
            {"objection_playbook": intelligence["objections"]}
            if intelligence.get("objections")
            else {}
        ),
        "allowed_tools": allowed_tools,
        "approval_required": [
            "custom_discount",
            "payment_collection",
            "contract_commitment",
        ],
        "stop_conditions": [
            "caller_requests_to_end_the_call",
            "caller_asks_not_to_be_contacted_again",
            *[
                _slug(reason, 60)
                for reason in (guardrails.get("disqualification_criteria") or [])[:3]
            ],
        ],
    }


# --------------------------------------------------------------------------
# End-to-end import
# --------------------------------------------------------------------------


def import_product(
    knowledge_base_path: Path | str,
    source_path: Path | str,
    *,
    task_id: str,
    sales_intelligence_path: Path | str | None = None,
    agent_name: str = "Adam",
    implemented_tools: set[str] | None = None,
    spoken_max_words: int = 30,
    spoken_sentence_limit: int = 2,
    strict: bool = False,
) -> ImportReport:
    """Verify, map and validate a crawled product into a task contract."""

    kb = json.loads(Path(knowledge_base_path).read_text(encoding="utf-8"))
    source = Path(source_path).read_text(encoding="utf-8")
    product = str(kb.get("product_name") or task_id)

    # Optional: how to sell it. Absent or unreadable leaves the agent accurate
    # but generic, which is a usable contract rather than a failed import.
    intelligence: dict[str, Any] = {}
    if sales_intelligence_path and Path(sales_intelligence_path).is_file():
        try:
            loaded = json.loads(Path(sales_intelligence_path).read_text(encoding="utf-8"))
            intelligence = loaded if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Ignoring unreadable sales intelligence: %s", exc)

    accepted: dict[str, str] = {}
    passed: list[FactCheck] = []
    rejected: list[FactCheck] = []
    for topic, fact in candidate_facts(kb).items():
        check = verify_fact(topic, fact, source)
        if check.grounded:
            accepted[topic] = fact
            passed.append(check)
        else:
            rejected.append(check)
            logger.warning("Dropped ungrounded fact %s: %s", topic, check.reason)

    blocking: list[str] = []
    warnings: list[str] = []

    # A dropped feature is survivable. A price or compliance claim that cannot be
    # traced means the crawl or the extraction is wrong about the thing that
    # matters most, and nothing downstream can repair that.
    for check in rejected:
        note = f"dropped {check.topic}: {check.reason}"
        if check.is_critical and strict:
            blocking.append(f"unverifiable {check.topic}: {check.reason}")
        else:
            warnings.append(note)

    # An objection answer is spoken aloud like any other claim, so any number in
    # one must trace to the source too. An ungrounded answer is dropped, not
    # blocked: losing an objection costs polish, not honesty.
    if intelligence.get("objections"):
        kept: list[dict[str, Any]] = []
        for item in intelligence["objections"]:
            answer = str(item.get("answer", ""))
            check = verify_fact(
                f"objection_{_slug(item.get('objection', ''))}",
                answer,
                source,
                require_proximity=False,
            )
            if check.grounded:
                kept.append(item)
            else:
                rejected.append(check)
                warnings.append(f"dropped objection answer: {check.reason}")
        intelligence = {**intelligence, "objections": kept}

    if len(accepted) < MIN_ACCEPTED_FACTS:
        blocking.append(
            f"only {len(accepted)} facts survived verification; "
            f"at least {MIN_ACCEPTED_FACTS} are needed to hold a sales conversation"
        )

    available = implemented_tools if implemented_tools is not None else set()
    contract = build_contract(
        kb,
        accepted,
        task_id=task_id,
        agent_name=agent_name,
        allowed_tools=sorted(available),
        spoken_max_words=spoken_max_words,
        spoken_sentence_limit=spoken_sentence_limit,
        sales_intelligence=intelligence,
    )
    if not available:
        warnings.append("no tools are implemented, so the agent can take no action on the call")

    try:
        contract = TaskEngine.validate_contract(contract)
    except ValueError as exc:
        blocking.append(f"the runtime rejected the generated contract: {exc}")

    return ImportReport(
        task_id=task_id,
        product_name=product,
        contract=contract,
        accepted=tuple(passed),
        rejected=tuple(rejected),
        blocking=tuple(blocking),
        warnings=tuple(warnings),
    )


def activate(report: ImportReport, engine: TaskEngine | None = None) -> bool:
    """Write the contract so the next call uses it, if every gate passed.

    Refusing here is the whole point: a blocked import leaves the previous
    contract in place rather than putting an unverified claim on a live call.
    """

    if not report.can_auto_apply:
        for reason in report.blocking:
            logger.error("Refusing to activate %s: %s", report.task_id, reason)
        return False
    (engine or TaskEngine()).save_contract(report.contract)
    logger.info(
        "Activated %s with %d verified facts", report.task_id, len(report.contract["knowledge"])
    )
    return True


def main(argv: list[str] | None = None) -> int:
    """Import a crawled product and activate it when it verifies."""

    import argparse

    from .tool_registry import load_user_tools, registered_tools

    parser = argparse.ArgumentParser(
        prog="phone-agent-import",
        description=(
            "Turn a ProductSearchEngine build into a live task contract. Facts that "
            "cannot be traced to the crawled source are dropped; an unverifiable "
            "price or compliance claim refuses activation outright."
        ),
    )
    parser.add_argument("--dist", required=True, type=Path,
                        help="ProductSearchEngine output directory")
    parser.add_argument("--task-id", required=True,
                        help="lowercase task id, e.g. vapi_platform_sales")
    parser.add_argument("--agent-name", default="Adam", help="the agent's spoken name")
    parser.add_argument("--activate", action="store_true",
                        help="write the contract when every gate passes")
    parser.add_argument(
        "--strict", action="store_true",
        help=(
            "refuse activation when a price or compliance claim will not verify. "
            "Off by default: the claim is dropped either way and never spoken."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_user_tools()
    report = import_product(
        args.dist / "product_knowledge_base.json",
        args.dist / "crawled_source.md",
        task_id=args.task_id,
        sales_intelligence_path=args.dist / "sales_intelligence.json",
        agent_name=args.agent_name,
        implemented_tools=set(registered_tools()),
        strict=args.strict,
    )

    print(report.summary())
    for reason in report.blocking:
        print(f"  BLOCKING  {reason}")
    for warning in report.warnings:
        print(f"  warning   {warning}")
    for check in report.rejected:
        print(f"  dropped   {check.topic}: {check.reason}")

    if not args.activate:
        print("\nRe-run with --activate to write this contract.")
        return 0 if report.can_auto_apply else 1
    return 0 if activate(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
