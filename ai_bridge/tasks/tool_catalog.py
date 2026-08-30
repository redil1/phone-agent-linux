"""Realtime function tools compiled from the active task contract.

Every spoken price, trial length or device claim is anchored to a verified
lookup rather than to contract text the model saw once at session start.

Tools here are strictly ones whose *result the model needs before it can
speak*. A tool call costs a second model inference before the caller hears
anything, so a write the model gains nothing from is not worth a turn of dead
air; those are observed from the transcript instead.

Only tools the contract allows *and* this module implements are ever offered.
The Realtime prompting guide is explicit that advertising a tool that does not
exist makes the model invent tool names and simulate actions.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .call_state import TaskRuntime
from .tool_registry import (
    DEFAULT_TIMEOUT_SECS,
    ToolSpec,
    load_user_tools,
    registered_tools,
    run_tool,
)

logger = logging.getLogger("RealtimeToolCatalog")

# One tool result should be a sentence or two of speakable fact, not a document.
MAX_RESULT_ITEMS = 4
# compile_realtime() inlines the whole fact base into the instructions, so below
# this size a lookup returns what the model is already holding and costs a second
# inference - heard on the call as a spoken preamble then dead air. Above it the
# facts no longer fit comfortably and retrieval earns its round trip. Sized well
# clear of a normal contract (~1.5k) so the two modes are not a coin flip at the
# boundary; gpt-realtime carries a 32k context, so 6k of facts is affordable.
INLINE_KNOWLEDGE_BUDGET_CHARS = 6_000
_STOPWORDS = frozenset(
    {
        "and", "the", "for", "are", "you", "your", "with", "have", "how", "what",
        "does", "did", "can", "any", "there", "this", "that", "des", "les", "une",
        "vous", "avez", "est", "que", "qui", "pour", "avec", "dans",
    }
)

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]

END_CALL_TOOL_NAME = "end_call"
MAX_END_CALL_REASON_CHARS = 240
MAX_END_CALL_CLOSING_CHARS = 320


@dataclass(frozen=True, slots=True)
class RealtimeTool:
    """One function exposed to the Realtime session."""

    name: str
    definition: dict[str, Any]
    handler: ToolHandler
    spec: ToolSpec | None = None
    timeout_secs: float = DEFAULT_TIMEOUT_SECS


def _function(name: str, description: str, properties: dict[str, Any], required: list[str]):
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def build_end_call_tool() -> RealtimeTool:
    """Build the model-owned, transport-neutral call completion control.

    The handler only validates bounded control data.  It deliberately does not
    decide whether the conversation is semantically finished; that decision is
    made by the Realtime model from the live conversation.  The voice pipeline
    consumes the accepted result, delivers the closing sentence, and only then
    asks the telephony host to hang up.
    """

    def request_end_call(arguments: dict[str, Any]) -> dict[str, Any]:
        reason = " ".join(str(arguments.get("reason", "")).split())
        closing_message = " ".join(str(arguments.get("closing_message", "")).split())
        if not reason:
            return {
                "accepted": False,
                "error": "reason is required",
                "say": "Choose whether the conversation is actually complete before retrying.",
            }
        if not closing_message:
            return {
                "accepted": False,
                "error": "closing_message is required",
                "say": "Provide one brief closing sentence in the caller's current language.",
            }
        if len(reason) > MAX_END_CALL_REASON_CHARS:
            return {"accepted": False, "error": "reason is too long"}
        if len(closing_message) > MAX_END_CALL_CLOSING_CHARS:
            return {"accepted": False, "error": "closing_message is too long"}
        return {
            "accepted": True,
            "reason": reason,
            "closing_message": closing_message,
            "status": "closing_message_will_be_spoken_before_hangup",
        }

    return RealtimeTool(
        name=END_CALL_TOOL_NAME,
        definition=_function(
            END_CALL_TOOL_NAME,
            "End this live phone call when you judge from the complete conversation that it "
            "should now finish. Use it for any genuinely final situation, such as the caller "
            "ending the conversation, a final refusal, or completed help followed by a natural "
            "close. Do not use it during a pause, while work or a tool is pending, when the "
            "caller may still need an answer, or merely because a business objective was met. "
            "Do not speak the closing separately: provide it here and PhoneAgent will speak it "
            "once, wait until the caller hears it, and then hang up.",
            {
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_END_CALL_REASON_CHARS,
                    "description": (
                        "A concise internal explanation of why the conversation is finished. "
                        "This is not spoken to the caller."
                    ),
                },
                "closing_message": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_END_CALL_CLOSING_CHARS,
                    "description": (
                        "One brief, natural, final closing sentence in the caller's current "
                        "language. It must not ask a question or open a new topic."
                    ),
                },
            },
            ["reason", "closing_message"],
        ),
        handler=request_end_call,
    )


def _knowledge(contract: dict[str, Any]) -> dict[str, str]:
    """Collect verified facts from every place a contract may put them.

    Contracts carry facts either as a `knowledge` mapping or as a
    `ground_truth_policy` list of "topic: fact" lines. Reading only the mapping
    left a contract that used the list with no searchable facts at all, while
    its pricing sat in the prompt where no lookup could reach it.
    """

    facts: dict[str, str] = {}
    knowledge = contract.get("knowledge")
    if isinstance(knowledge, dict):
        facts.update({str(key): str(value) for key, value in knowledge.items()})
    for index, entry in enumerate(contract.get("ground_truth_policy") or []):
        text = str(entry).strip()
        if not text:
            continue
        topic, separator, fact = text.partition(":")
        if separator and fact.strip() and " " not in topic.strip():
            facts.setdefault(topic.strip(), fact.strip())
        else:
            facts.setdefault(f"verified_fact_{index + 1}", text)
    return facts


def _plan_names(knowledge: dict[str, str]) -> list[str]:
    """Derive plan identifiers from the contract's own price keys."""

    names = [key[: -len("_price")] for key in knowledge if key.endswith("_price")]
    return sorted(names)


def build_tool_catalog(
    contract: dict[str, Any],
    task: TaskRuntime,
) -> dict[str, RealtimeTool]:
    """Return the tools this contract allows and this runtime can honour."""

    knowledge = _knowledge(contract)
    allowed = {str(name) for name in contract.get("allowed_tools", []) or []}
    approval_required = {str(name) for name in contract.get("approval_required", []) or []}
    inlined = sum(len(key) + len(value) for key, value in knowledge.items())
    retrieval_is_useful = inlined > INLINE_KNOWLEDGE_BUDGET_CHARS
    catalog: dict[str, RealtimeTool] = {}

    # ---- knowledge_base_search -------------------------------------------------
    def knowledge_base_search(arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip().lower()
        if not query:
            return {"found": False, "reason": "empty query"}
        # Whole words only. Substring matching scored "devices" against the
        # term "and" because of "Android", burying the fact actually asked for.
        terms = {
            term
            for term in re.findall(r"[\wÀ-ÿ]+", query)
            if len(term) > 2 and term not in _STOPWORDS
        }
        scored: list[tuple[int, str, str]] = []
        for key, value in knowledge.items():
            words = set(re.findall(r"[\wÀ-ÿ]+", f"{key} {value}".lower()))
            score = len(terms & words)
            if score:
                scored.append((score, key, value))
        scored.sort(key=lambda row: (-row[0], row[1]))
        if not scored:
            return {
                "found": False,
                "guidance": "No verified fact covers this. Say so briefly; do not improvise.",
            }
        return {
            "found": True,
            "facts": [{"topic": key, "fact": value} for _, key, value in scored[:MAX_RESULT_ITEMS]],
        }

    if retrieval_is_useful:
        catalog["knowledge_base_search"] = RealtimeTool(
            name="knowledge_base_search",
            definition=_function(
                "knowledge_base_search",
                "Look up a verified product fact before stating it. Use this whenever the "
                "caller asks about the service and you are not certain of the answer.",
                {
                    "query": {
                        "type": "string",
                        "description": "What the caller wants to know, in a few words.",
                    }
                },
                ["query"],
            ),
            handler=knowledge_base_search,
        )

    # ---- subscription_plan_lookup ---------------------------------------------
    plans = _plan_names(knowledge)

    def subscription_plan_lookup(arguments: dict[str, Any]) -> dict[str, Any]:
        requested = str(arguments.get("plan", "all")).strip().lower()
        wanted = plans if requested in ("", "all") else [requested]
        found = {
            plan: knowledge[f"{plan}_price"] for plan in wanted if f"{plan}_price" in knowledge
        }
        if not found:
            return {"found": False, "available_plans": plans}
        extras = {
            key: knowledge[key]
            for key in ("yearly_discount", "trial", "devices")
            if key in knowledge
        }
        return {"found": True, "plans": found, "also_verified": extras}

    if plans and retrieval_is_useful:
        catalog["subscription_plan_lookup"] = RealtimeTool(
            name="subscription_plan_lookup",
            definition=_function(
                "subscription_plan_lookup",
                "Get verified pricing and what each plan includes. Call this before "
                "quoting any price.",
                {
                    "plan": {
                        "type": "string",
                        "enum": [*plans, "all"],
                        "description": "Which plan to price, or 'all' to compare.",
                    }
                },
                ["plan"],
            ),
            handler=subscription_plan_lookup,
        )

    # ---- lead_capture: deliberately NOT a tool -------------------------------
    # Every tool call costs a second model inference before the caller hears
    # anything, and the model needs nothing back from a write. Offering this on
    # the most frequent turn type - answering a discovery question - added a
    # spoken preamble plus seconds of dead air per turn. Slots are filled from
    # the caller transcript in AgentPolicyRuntime.observe_transcription instead,
    # which is now a reporting concern rather than a control input.

    # ---- callback_schedule -----------------------------------------------------
    def callback_schedule(arguments: dict[str, Any]) -> dict[str, Any]:
        when = str(arguments.get("when", "")).strip()
        if not when:
            return {"recorded": False, "reason": "no time given"}
        task.record("callback_request", when)
        # There is no calendar integration. Saying "booked" would be a false
        # claim of a completed action, which the contract forbids.
        return {
            "recorded": True,
            "when": when,
            "status": "noted_for_operator_confirmation",
            "say": "Tell the caller the request is noted and an operator will confirm it.",
        }

    catalog["callback_schedule"] = RealtimeTool(
        name="callback_schedule",
        definition=_function(
            "callback_schedule",
            "Note a time the caller asked to be called back. This records the request "
            "for an operator; it does not confirm an appointment.",
            {
                "when": {
                    "type": "string",
                    "description": "The time the caller asked for, in their words.",
                }
            },
            ["when"],
        ),
        handler=callback_schedule,
    )

    # ---- gated actions ---------------------------------------------------------
    def refuse_gated(action: str) -> ToolHandler:
        def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            logger.info("Realtime tool %s requires operator approval args=%s", action, arguments)
            return {
                "completed": False,
                "reason": "requires_authorized_operator",
                "say": (
                    "Tell the caller you will set this up with an authorized colleague "
                    "and confirm shortly. Never say it is already done."
                ),
            }

        return handler

    for action in sorted(approval_required & allowed):
        catalog[action] = RealtimeTool(
            name=action,
            definition=_function(
                action,
                f"Request {action.replace('_', ' ')}. This always requires an authorized "
                "operator and never completes on this call.",
                {
                    "summary": {
                        "type": "string",
                        "description": "What the caller agreed to, in one short sentence.",
                    }
                },
                ["summary"],
            ),
            handler=refuse_gated(action),
        )

    # ---- user-defined tools ----------------------------------------------------
    # These do real work: database reads, internal APIs, actions. The contract
    # still decides what this call may use, so a tool being importable is not
    # the same as it being permitted.
    load_user_tools()
    for name, spec in registered_tools().items():
        if name not in allowed:
            logger.info(
                "User tool %r is loaded but not in allowed_tools for this contract", name
            )
            continue
        if name in catalog:
            logger.warning("User tool %r overrides the built-in of the same name", name)
        catalog[name] = RealtimeTool(
            name=name,
            definition=spec.definition,
            handler=None,  # type: ignore[arg-type]
            spec=spec,
            timeout_secs=spec.timeout_secs,
        )

    catalog = {name: tool for name, tool in catalog.items() if name in allowed}
    # A contract that promises a tool nobody implemented is how an agent ends up
    # offering a caller a checkout it cannot perform.
    missing = unimplemented_tools(contract, catalog)
    if missing:
        logger.warning(
            "Task contract allows tools with no implementation: %s. The agent cannot "
            "perform these; add them under ~/.config/phone-agent/tools/ or remove them "
            "from allowed_tools.",
            ", ".join(missing),
        )
    logger.info(
        "Realtime tool catalog ready tools=%s inlined_knowledge_chars=%d retrieval=%s",
        sorted(catalog),
        inlined,
        retrieval_is_useful,
    )
    return catalog


# Tools this module knows about and may deliberately withhold: retrieval when the
# facts are already inlined, and lead_capture always. Reporting those as missing
# would send an operator hunting for something intentionally absent.
KNOWN_BUILT_IN_TOOLS = frozenset(
    {
        "knowledge_base_search",
        "subscription_plan_lookup",
        "lead_capture",
        "callback_schedule",
    }
)


def unimplemented_tools(contract: dict[str, Any], catalog: dict[str, RealtimeTool]) -> list[str]:
    """Contract tools nothing in this system implements, for the Studio to surface.

    This names only genuinely absent tools - the ones that make the agent promise
    an action it cannot perform.
    """

    allowed = {str(name) for name in contract.get("allowed_tools", []) or []}
    approval_required = {str(name) for name in contract.get("approval_required", []) or []}
    accounted_for = set(catalog) | KNOWN_BUILT_IN_TOOLS | approval_required
    return sorted(allowed - accounted_for)


def tool_definitions(catalog: dict[str, RealtimeTool]) -> list[dict[str, Any]]:
    return [tool.definition for tool in catalog.values()]


async def execute_tool(
    catalog: dict[str, RealtimeTool],
    name: str,
    raw_arguments: str,
) -> str:
    """Run one tool call and return the JSON string the model expects back.

    Nothing raised in here may reach the call. The caller is mid-conversation
    waiting for speech, so every failure becomes a result the model can talk
    about instead of an exception that ends the turn.
    """

    tool = catalog.get(name)
    if tool is None:
        return json.dumps({"error": f"unknown tool {name}"})
    try:
        arguments = json.loads(raw_arguments) if raw_arguments else {}
    except json.JSONDecodeError:
        return json.dumps({"error": "arguments were not valid JSON"})
    if not isinstance(arguments, dict):
        return json.dumps({"error": "arguments must be a JSON object"})
    try:
        if tool.spec is not None:
            result = await run_tool(tool.spec, arguments)
        else:
            result = tool.handler(arguments)
    except Exception as exc:  # a tool failure must not end the call
        logger.exception("Realtime tool failed name=%s", name)
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
    if not isinstance(result, dict):
        result = {"result": result}
    return json.dumps(result, ensure_ascii=False, default=str)
