"""Caller-bound Frappe CRM, Helpdesk and ERPNext tools for live calls.

PhoneAgent remains the conversational decision maker.  Frappe is the durable
system of record.  Every live-call tool is bound to the authenticated current
caller, returns bounded JSON, and delegates business invariants to the bundled
``phoneagent_frappe`` app.  The model never receives API credentials and cannot
select an arbitrary customer phone number.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .mcp_broker import _sanitize
from .secure_storage import atomic_write_private, harden_private_file
from .tasks.task_engine import TASK_ID_RE
from .tasks.tool_catalog import RealtimeTool
from .tasks.tool_registry import ToolSpec
from .tool_control import MASKED_SECRET

DEFAULT_FRAPPE_CONFIG_PATH = Path.home() / ".config" / "phone-agent" / "frappe.json"
MAX_FRAPPE_RESPONSE_BYTES = 512 * 1024
MAX_TOOL_TEXT = 2_000

FRAPPE_TOOL_NAMES = (
    "business_get_customer_context",
    "business_upsert_current_lead",
    "business_record_call_outcome",
    "business_create_opportunity",
    "business_schedule_follow_up",
    "business_search_catalog",
    "business_create_quotation_draft",
    "business_create_sales_order_draft",
    "business_get_order_status",
    "business_get_invoice_status",
    "business_create_support_ticket",
    "business_get_support_status",
    "business_update_support_ticket",
    "business_mark_do_not_call",
)


class FrappeIntegrationError(RuntimeError):
    pass


class FrappeToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = False
    task_ids: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("name")
    @classmethod
    def _known_name(cls, value: str) -> str:
        if value not in FRAPPE_TOOL_NAMES:
            raise ValueError("unknown Frappe business tool")
        return value

    @field_validator("task_ids")
    @classmethod
    def _valid_tasks(cls, values: list[str]) -> list[str]:
        unique = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        if any(value != "*" and not TASK_ID_RE.fullmatch(value) for value in unique):
            raise ValueError("Frappe tool task ids are invalid")
        return unique


def _default_tool_policies() -> list[FrappeToolPolicy]:
    return [FrappeToolPolicy(name=name) for name in FRAPPE_TOOL_NAMES]


class FrappeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    enabled: bool = False
    base_url: str = "http://127.0.0.1:8080"
    site_name: str = "phoneagent.localhost"
    api_key: str = Field(default="", max_length=512)
    api_secret: str = Field(default="", max_length=2_048)
    request_timeout_ms: int = Field(default=8_000, ge=500, le=30_000)
    max_result_items: int = Field(default=10, ge=1, le=50)
    campaign_autopilot_enabled: bool = False
    campaign_poll_seconds: int = Field(default=15, ge=5, le=300)
    campaign_claim_seconds: int = Field(default=300, ge=60, le=900)
    tools: list[FrappeToolPolicy] = Field(default_factory=_default_tool_policies)

    @field_validator("base_url")
    @classmethod
    def _safe_base_url(cls, value: str) -> str:
        parsed = urlsplit(value.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Frappe URL must use http or https")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Frappe URL cannot contain credentials, query or fragment")
        if parsed.scheme == "http" and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("remote Frappe servers must use HTTPS")
        return value.rstrip("/")

    @field_validator("site_name")
    @classmethod
    def _safe_site_name(cls, value: str) -> str:
        name = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", name) or ".." in name:
            raise ValueError("Frappe site name is invalid")
        return name

    @model_validator(mode="after")
    def _complete_tools_and_credentials(self) -> FrappeConfig:
        by_name = {tool.name: tool for tool in self.tools}
        if len(by_name) != len(self.tools):
            raise ValueError("Frappe tools must be unique")
        self.tools = [
            by_name.get(name) or FrappeToolPolicy(name=name) for name in FRAPPE_TOOL_NAMES
        ]
        if self.enabled and (not self.api_key or not self.api_secret):
            raise ValueError("enabled Frappe integration requires an API key and secret")
        return self


class FrappeConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(
            os.getenv("PHONE_AGENT_FRAPPE_CONFIG", "").strip() or DEFAULT_FRAPPE_CONFIG_PATH
        )

    def load(self) -> FrappeConfig:
        if not self.path.exists():
            return FrappeConfig()
        harden_private_file(self.path)
        try:
            return FrappeConfig.model_validate(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise FrappeIntegrationError(f"Frappe configuration is invalid: {exc}") from exc

    def save(self, payload: dict[str, Any]) -> FrappeConfig:
        previous = self.load()
        candidate = dict(payload)
        candidate.pop("fingerprint", None)
        if candidate.get("api_key") in {MASKED_SECRET, "", None} and previous.api_key:
            candidate["api_key"] = previous.api_key
        if candidate.get("api_secret") in {MASKED_SECRET, "", None} and previous.api_secret:
            candidate["api_secret"] = previous.api_secret
        config = FrappeConfig.model_validate(candidate)
        config.revision = previous.revision + 1
        atomic_write_private(
            self.path,
            json.dumps(config.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        )
        return config

    def hydrate(self, payload: dict[str, Any]) -> FrappeConfig:
        previous = self.load()
        candidate = dict(payload)
        candidate.pop("fingerprint", None)
        if candidate.get("api_key") in {MASKED_SECRET, "", None} and previous.api_key:
            candidate["api_key"] = previous.api_key
        if candidate.get("api_secret") in {MASKED_SECRET, "", None} and previous.api_secret:
            candidate["api_secret"] = previous.api_secret
        return FrappeConfig.model_validate(candidate)

    def public_state(self) -> dict[str, Any]:
        payload = self.load().model_dump(mode="json")
        payload["api_key"] = MASKED_SECRET if payload["api_key"] else ""
        payload["api_secret"] = MASKED_SECRET if payload["api_secret"] else ""
        payload["fingerprint"] = self.fingerprint()
        return payload

    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.load().model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


def _phone_digits(value: str) -> str:
    text = str(value).strip()
    if text.startswith("unknown:") or text == "anonymous":
        return ""
    digits = "".join(character for character in text if character.isdigit())
    return digits if 7 <= len(digits) <= 15 else ""


class FrappeClient:
    def __init__(self, config: FrappeConfig, session: aiohttp.ClientSession) -> None:
        self.config = config
        self.session = session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Any:
        headers = {
            "Accept": "application/json",
            "X-Frappe-Site-Name": self.config.site_name,
        }
        if authenticated:
            headers["Authorization"] = f"token {self.config.api_key}:{self.config.api_secret}"
        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_ms / 1_000)
        async with self.session.request(
            method,
            f"{self.config.base_url}{path}",
            json=payload,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        ) as response:
            if 300 <= response.status < 400:
                raise FrappeIntegrationError("Frappe redirect was refused")
            raw = await response.content.read(MAX_FRAPPE_RESPONSE_BYTES + 1)
            if len(raw) > MAX_FRAPPE_RESPONSE_BYTES:
                raise FrappeIntegrationError("Frappe response exceeded its bound")
            try:
                body = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FrappeIntegrationError("Frappe returned invalid JSON") from exc
            if not 200 <= response.status < 300:
                if isinstance(body, dict):
                    detail = body.get("message") or body.get("exception") or body.get("exc_type")
                else:
                    detail = None
                raise FrappeIntegrationError(
                    f"Frappe returned {response.status}: {str(detail or 'request failed')[:300]}"
                )
            return body

    async def call(self, method: str, arguments: dict[str, Any] | None = None) -> Any:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", method):
            raise FrappeIntegrationError("invalid Frappe method")
        body = await self._request(
            "POST",
            f"/api/method/phoneagent_frappe.api.{quote(method, safe='')}",
            payload=arguments or {},
        )
        if not isinstance(body, dict) or "message" not in body:
            raise FrappeIntegrationError("Frappe method response is invalid")
        return _sanitize(body["message"])

    async def health(self) -> dict[str, Any]:
        result = await self.call("health")
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise FrappeIntegrationError("Frappe PhoneAgent app is not ready")
        return result


class FrappeToolRuntime:
    def __init__(
        self,
        config: FrappeConfig,
        *,
        caller_id: str,
        task_id: str,
        call_id: str,
        call_direction: str,
        event_sink: Any | None = None,
    ) -> None:
        self.config = config
        self.caller_id = caller_id
        self.task_id = task_id
        self.call_id = str(call_id)
        self.call_direction = call_direction
        self.event_sink = event_sink
        self.phone_digits = _phone_digits(caller_id)
        self.session: aiohttp.ClientSession | None = None
        self.client: FrappeClient | None = None
        self.catalog: dict[str, RealtimeTool] = {}

    async def start(self) -> dict[str, RealtimeTool]:
        if not self.config.enabled:
            return {}
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.request_timeout_ms / 1_000)
        )
        self.client = FrappeClient(self.config, self.session)
        try:
            health = await asyncio.wait_for(self.client.health(), timeout=1.5)
        except Exception as exc:
            await self._emit(
                {"type": "frappe_runtime_status", "state": "unavailable", "message": str(exc)[:300]}
            )
            await self.close()
            return {}
        policies = {policy.name: policy for policy in self.config.tools}
        for name in FRAPPE_TOOL_NAMES:
            policy = policies[name]
            if policy.enabled and (
                not policy.task_ids or "*" in policy.task_ids or self.task_id in policy.task_ids
            ):
                self._register_tool(name)
        await self._emit(
            {
                "type": "frappe_runtime_status",
                "state": "ready",
                "tools": sorted(self.catalog),
                "site": health.get("site"),
            }
        )
        return dict(self.catalog)

    def _register_tool(self, name: str) -> None:
        definition = self._definitions()[name]

        async def handler(**arguments: Any) -> dict[str, Any]:
            return await self._execute(name, arguments)

        spec = ToolSpec(
            name=name,
            description=definition["description"],
            handler=handler,
            params=definition["parameters"]["properties"],
            required=tuple(definition["parameters"]["required"]),
            timeout_secs=self.config.request_timeout_ms / 1_000 + 2,
        )
        self.catalog[name] = RealtimeTool(
            name=name,
            definition=spec.definition,
            handler=None,  # type: ignore[arg-type]
            spec=spec,
            timeout_secs=spec.timeout_secs,
        )

    async def _execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        client = self._require_client()
        method_map = {
            "business_get_customer_context": "get_customer_context",
            "business_upsert_current_lead": "upsert_lead",
            "business_record_call_outcome": "record_call_outcome",
            "business_create_opportunity": "create_opportunity",
            "business_schedule_follow_up": "schedule_follow_up",
            "business_search_catalog": "search_catalog",
            "business_create_quotation_draft": "create_quotation_draft",
            "business_create_sales_order_draft": "create_sales_order_draft",
            "business_get_order_status": "get_order_status",
            "business_get_invoice_status": "get_invoice_status",
            "business_create_support_ticket": "create_support_ticket",
            "business_get_support_status": "get_support_status",
            "business_update_support_ticket": "update_support_ticket",
            "business_mark_do_not_call": "mark_do_not_call",
        }
        method = method_map.get(name)
        if method is None:
            raise FrappeIntegrationError(f"unknown Frappe tool {name}")
        phone_to_use = self.phone_digits or _phone_digits(
            str(arguments.get("phone_number") or arguments.get("phone") or "")
        )
        payload = {
            **arguments,
            "phone": phone_to_use,
            "call_id": self.call_id,
            "task_id": self.task_id,
            "call_direction": self.call_direction,
            "max_items": self.config.max_result_items,
        }
        result = await client.call(method, payload)
        await self._emit(
            {
                "type": "frappe_tool_complete",
                "name": name,
                "verified": bool(isinstance(result, dict) and result.get("verified")),
            }
        )
        return result if isinstance(result, dict) else {"verified": True, "result": result}

    @staticmethod
    def _definitions() -> dict[str, dict[str, Any]]:
        def function(
            name: str,
            description: str,
            properties: dict[str, Any],
            required: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "type": "function",
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required or [],
                    "additionalProperties": False,
                },
            }

        text = {"type": "string", "maxLength": MAX_TOOL_TEXT}
        short = {"type": "string", "maxLength": 240}
        return {
            "business_get_customer_context": function(
                "business_get_customer_context",
                "Load verified CRM, sales, order, invoice, subscription and support context for "
                "the authenticated current caller. Use before claiming what the business knows "
                "about this customer. The caller is selected securely by PhoneAgent.",
                {},
            ),
            "business_upsert_current_lead": function(
                "business_upsert_current_lead",
                "Create or update the current caller as a CRM lead using details they provided. "
                "Never invent a name, email, company, consent or need.",
                {
                    "name": short,
                    "email": {"type": "string", "maxLength": 320},
                    "company": short,
                    "notes": text,
                    "consent_status": {
                        "type": "string",
                        "enum": ["unknown", "consented", "declined", "do_not_call"],
                    },
                },
            ),
            "business_record_call_outcome": function(
                "business_record_call_outcome",
                "Record a verified disposition and concise summary for this live call. Use when "
                "the outcome becomes clear or before ending. Do not call a prospect interested "
                "unless their words support it.",
                {
                    "disposition": {
                        "type": "string",
                        "enum": [
                            "connected",
                            "interested",
                            "qualified",
                            "converted",
                            "callback",
                            "not_interested",
                            "do_not_call",
                            "support_resolved",
                            "support_open",
                            "no_answer",
                            "failed",
                        ],
                    },
                    "summary": text,
                    "next_action": short,
                    "follow_up_at": {"type": "string", "maxLength": 64},
                },
                ["disposition", "summary"],
            ),
            "business_create_opportunity": function(
                "business_create_opportunity",
                "Create a CRM sales opportunity for the current caller only after they express "
                "real interest. This records a pipeline opportunity; it does not complete a sale.",
                {
                    "title": short,
                    "notes": text,
                    "estimated_value": {"type": "number", "minimum": 0},
                    "currency": {"type": "string", "minLength": 3, "maxLength": 3},
                    "probability": {"type": "number", "minimum": 0, "maximum": 100},
                },
                ["title"],
            ),
            "business_schedule_follow_up": function(
                "business_schedule_follow_up",
                "Schedule a CRM follow-up for the current caller at the time they requested or "
                "accepted. This creates a task, not a guaranteed external appointment.",
                {
                    "at": {"type": "string", "maxLength": 64},
                    "description": text,
                    "channel": {"type": "string", "enum": ["phone", "whatsapp", "email"]},
                },
                ["at", "description"],
            ),
            "business_search_catalog": function(
                "business_search_catalog",
                "Search verified ERPNext products, services, prices and stock. Use before quoting "
                "a catalog fact that is not already verified in the active task.",
                {"query": {"type": "string", "minLength": 2, "maxLength": 240}},
                ["query"],
            ),
            "business_create_quotation_draft": function(
                "business_create_quotation_draft",
                "Create a non-binding draft quotation for the current caller from verified item "
                "codes and quantities. Never say it is submitted, accepted or paid.",
                {
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "properties": {
                                "item_code": {"type": "string", "maxLength": 140},
                                "quantity": {"type": "number", "exclusiveMinimum": 0},
                            },
                            "required": ["item_code", "quantity"],
                            "additionalProperties": False,
                        },
                    },
                    "notes": text,
                },
                ["items"],
            ),
            "business_create_sales_order_draft": function(
                "business_create_sales_order_draft",
                "Create a draft sales order from a verified draft quotation after the caller "
                "clearly agrees to proceed. It does not submit, invoice, charge or activate.",
                {"quotation_id": {"type": "string", "maxLength": 140}, "notes": text},
                ["quotation_id"],
            ),
            "business_get_order_status": function(
                "business_get_order_status",
                "Get verified recent sales-order and fulfillment status for the current caller.",
                {"order_id": {"type": "string", "maxLength": 140}},
            ),
            "business_get_invoice_status": function(
                "business_get_invoice_status",
                "Get verified recent invoice, outstanding balance and payment status for the "
                "current caller. Never infer payment from intent.",
                {"invoice_id": {"type": "string", "maxLength": 140}},
            ),
            "business_create_support_ticket": function(
                "business_create_support_ticket",
                "Open a customer-service ticket for the current caller with their actual issue, "
                "priority and relevant details. Copy any caller-dictated title and description "
                "exactly; never paraphrase proper names or supplied wording.",
                {
                    "subject": short,
                    "description": text,
                    "priority": {"type": "string", "enum": ["Low", "Medium", "High", "Urgent"]},
                    "category": short,
                },
                ["subject", "description"],
            ),
            "business_get_support_status": function(
                "business_get_support_status",
                "Get verified open and recent support-ticket status for the current caller.",
                {"ticket_id": {"type": "string", "maxLength": 140}},
            ),
            "business_update_support_ticket": function(
                "business_update_support_ticket",
                "Add the caller's new information to one of their own support tickets. Only set "
                "Resolved when the caller confirms the issue is resolved. Copy a dictated comment "
                "exactly without paraphrasing.",
                {
                    "ticket_id": {"type": "string", "maxLength": 140},
                    "comment": text,
                    "status": {"type": "string", "enum": ["Open", "Replied", "Resolved"]},
                },
                ["ticket_id", "comment"],
            ),
            "business_mark_do_not_call": function(
                "business_mark_do_not_call",
                "Immediately record the current caller's request not to receive future calls. "
                "Use whenever the caller clearly opts out.",
                {"reason": short},
                ["reason"],
            ),
        }

    def _require_client(self) -> FrappeClient:
        if self.client is None:
            raise FrappeIntegrationError("Frappe business suite is not running")
        return self.client

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None
        self.client = None
        self.catalog.clear()

    async def _emit(self, event: dict[str, Any]) -> None:
        if self.event_sink is None:
            return
        result = self.event_sink(event)
        if inspect.isawaitable(result):
            await result
