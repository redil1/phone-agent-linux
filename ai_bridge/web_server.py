"""Local PhoneAgent Studio server with live call events and configuration."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import WSMsgType, web

from .control_plane import (
    AgentPackage,
    ControlPlaneError,
    ControlPlaneStore,
    PackageValidation,
    RuntimeControl,
    package_hash,
    state_hash,
)
from .edge_voice_catalog import fallback_edge_voice_catalog, fetch_edge_voice_catalog
from .frappe_integration import (
    FrappeClient,
    FrappeConfigStore,
    FrappeIntegrationError,
)
from .identity.models import MemoryBlock
from .identity.skills import SkillDraft, SkillError
from .identity.store import IdentityStoreError
from .local_control import load_or_create_control_token
from .mcp_broker import McpBrokerError
from .memory.memory_manager import LayeredMemoryManager
from .openwa_integration import (
    OpenWAClient,
    OpenWAConfigStore,
    OpenWAError,
)
from .pairing import build_pairing, key_fingerprint, pairing_status
from .personality.persona_compiler import PersonaCompiler
from .production_security import AuditLedger, CallPolicy, public_destination
from .remote_link import (
    RemoteLinkRelay,
    RemoteLinkSettings,
    load_remote_link_key,
    local_addresses,
)
from .runtime_config import ProviderConfig
from .secure_storage import append_private_line, atomic_write_private, harden_private_file
from .tasks.task_engine import TaskEngine
from .tasks.tool_registry import load_user_tools, registered_tools
from .tool_control import (
    ToolApprovalQueue,
    ToolControlError,
    ToolControlStore,
    test_connection,
)
from .web_research import (
    WebResearchConfigStore,
    WebResearchEngine,
    WebResearchError,
)

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "web_static"
EVENT_PREFIX = "PHONE_AGENT_EVENT "
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8090
EDGE_VOICE_CACHE_SECONDS = 6 * 60 * 60
EDGE_VOICE_FALLBACK_RETRY_SECONDS = 30
APPROVAL_TTL_SECONDS = 300


RAW_CHILD_LOG = Path.home() / "phone-agent-logs" / "voice-host-raw.log"


def _environment_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _loopback_host(value: str) -> bool:
    """Whether this host may address the Studio.

    ``PHONE_AGENT_ALLOW_EXTERNAL`` defaults to false. Defaulting it true made
    this function return true for every hostname with no configuration at all,
    which silently disabled both the bind guard and the DNS-rebinding check.
    Deployments that genuinely serve a non-loopback interface set the variable
    explicitly (the production compose file does), so the safe default costs
    them nothing.
    """

    host = value.strip().lower().strip("[]")
    if host in {"127.0.0.1", "localhost", "::1"}:
        return True
    if _environment_bool("PHONE_AGENT_ALLOW_EXTERNAL", False):
        return True
    allowed = {
        h.strip().lower()
        for h in os.getenv("PHONE_AGENT_ALLOWED_HOSTS", "").split(",")
        if h.strip()
    }
    return host in allowed


@web.middleware
async def local_security_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Reject DNS rebinding and cross-origin browser mutations."""

    host_name = request.host
    if host_name.startswith("["):
        host_name = host_name.split("]", 1)[0] + "]"
    elif ":" in host_name:
        host_name = host_name.rsplit(":", 1)[0]
    if not _loopback_host(host_name):
        return web.json_response({"status": "error", "message": "invalid Host"}, status=421)
    origin = request.headers.get("Origin")
    browser_mutation = request.method not in {"GET", "HEAD", "OPTIONS"} or request.path == "/ws"
    if origin and browser_mutation:
        parsed = urlsplit(origin)
        expected = f"{request.scheme}://{request.host}"
        if not _loopback_host(parsed.hostname or "") or origin.rstrip("/") != expected:
            return web.json_response(
                {"status": "error", "message": "cross-origin request refused"}, status=403
            )
    response = await handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; "
        "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' "
        "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
        "connect-src 'self' ws: wss:; img-src 'self' data:"
    )
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@dataclass(slots=True)
class PendingApproval:
    request_id: str
    destination: str
    public_destination: str
    recording_consent: bool
    created_at: float
    state: str = "pending"


def _write_raw_child_line_to(path: Path, line: str) -> None:
    """Append one line of the voice host's own output, exactly as printed."""

    try:
        append_private_line(path, line)
    except OSError:
        # Diagnostics must never take the call down with them.
        pass


def _looks_like_exception(line: str) -> bool:
    """Whether a line is the exception that closes a traceback.

    It is the one line of a traceback that carries no indentation, so it cannot
    be recognised by shape alone — it is matched by the ``Error: message`` form
    Python prints.
    """

    head = line.split(":", 1)[0].strip()
    return bool(head) and head[:1].isupper() and " " not in head


def _sanitize_openwa(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "<truncated>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:64]:
            name = str(key)[:100]
            result[name] = (
                "<redacted>"
                if re.search(r"key|secret|token|password|credential", name, re.I)
                else _sanitize_openwa(item, depth=depth + 1)
            )
        return result
    if isinstance(value, list):
        return [_sanitize_openwa(item, depth=depth + 1) for item in value[:64]]
    if isinstance(value, str):
        return value[:2_000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def _public_openwa_session(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    allowed = (
        "id",
        "name",
        "status",
        "phone",
        "isActive",
        "engineType",
        "createdAt",
        "updatedAt",
    )
    return {name: _sanitize_openwa(value[name]) for name in allowed if name in value}


def _openwa_session_ready(value: dict[str, Any] | None) -> bool:
    if not value:
        return False
    return str(value.get("status") or "").lower() in {
        "ready",
        "connected",
        "authenticated",
    }


class PhoneAgentWebServer:
    """Control only the call process created by this Studio instance."""

    CONFIG_FIELDS = frozenset(
        {
            "tts_provider",
            "tts_model",
            "tts_voice_id",
            "llm_provider",
            "llm_model",
            "vllm_base_url",
            "lmstudio_base_url",
            "stt_provider",
            "stt_model",
            "stt_language",
            "tts_aggregation",
            "pipeline_mode",
            "chatgpt_realtime_voice",
            "chatgpt_realtime_model",
            "chatgpt_realtime_transport",
            "chatgpt_realtime_reasoning_effort",
            "chatgpt_realtime_vad_mode",
            "call_channel",
        }
    )
    BOOLEAN_CONFIG_FIELDS = frozenset(
        {"speculative_pipeline_enabled", "conversational_reflex_enabled"}
    )
    GOOGLE_TTS_TEXT_FIELDS = frozenset({"google_tts_scene", "google_tts_sample_context"})

    def __init__(
        self,
        *,
        host: str = DEFAULT_WEB_HOST,
        port: int = DEFAULT_WEB_PORT,
        config: ProviderConfig | None = None,
        persona_compiler: PersonaCompiler | None = None,
        memory_manager: LayeredMemoryManager | None = None,
        task_engine: TaskEngine | None = None,
        settings_path: Path | None = None,
        call_policy: CallPolicy | None = None,
        audit_ledger: AuditLedger | None = None,
        tool_control_store: ToolControlStore | None = None,
        tool_approval_queue: ToolApprovalQueue | None = None,
        openwa_config_store: OpenWAConfigStore | None = None,
        web_research_config_store: WebResearchConfigStore | None = None,
        frappe_config_store: FrappeConfigStore | None = None,
        control_plane_store: ControlPlaneStore | None = None,
    ) -> None:
        self.host = host
        self.port = port
        if not _loopback_host(host):
            raise ValueError("PhoneAgent Studio must bind to a loopback address")
        self.config = config or ProviderConfig.from_env(require_credentials=False)
        self.task_id = os.getenv("PHONE_AGENT_TASK_ID", "iptv_subscription_sales").strip()
        self.system_prompt = os.getenv("PHONE_AGENT_SYSTEM_PROMPT", "").strip()
        self.auto_answer_enabled = _environment_bool("PHONE_AGENT_AUTO_ANSWER", False)
        self.settings_path = settings_path or (
            Path.home() / ".config" / "phone-agent" / "studio.json"
        )
        self.persona_compiler = persona_compiler or PersonaCompiler()
        self.identity_kernel = self.persona_compiler.identity_kernel
        self.memory_manager = memory_manager or LayeredMemoryManager()
        self.task_engine = task_engine or TaskEngine()
        self.call_policy = call_policy or CallPolicy()
        self.audit_ledger = audit_ledger or AuditLedger()
        self.tool_control_store = tool_control_store or ToolControlStore()
        self.tool_approval_queue = tool_approval_queue or ToolApprovalQueue()
        self.openwa_config_store = openwa_config_store or OpenWAConfigStore()
        self.web_research_config_store = (
            web_research_config_store or WebResearchConfigStore()
        )
        self.frappe_config_store = frappe_config_store or FrappeConfigStore()
        self.control_plane_store = control_plane_store or ControlPlaneStore()
        self._control_token = load_or_create_control_token()
        self._approvals: dict[str, PendingApproval] = {}
        if config is None:
            self._load_saved_settings()

        self.app = web.Application(middlewares=[local_security_middleware])
        self.app.on_startup.append(self._on_startup)
        self.app.on_shutdown.append(self._on_shutdown)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._ws_clients: set[web.WebSocketResponse] = set()
        self._active_process: asyncio.subprocess.Process | None = None
        self._dial_task: asyncio.Task[None] | None = None
        self._receptionist_process: asyncio.subprocess.Process | None = None
        # When a handset tunnels in, the relay presents its gateway ports on
        # this machine's loopback, so the voice host keeps talking to
        # 127.0.0.1:8765-8768 and never learns the cable is gone.
        self._remote_link_settings = RemoteLinkSettings.load()
        self._remote_link: RemoteLinkRelay | None = None
        self._remote_link_error = ""
        # A warm host does not exit when its call ends, so the end of a call is
        # learned from its published state transitions instead.
        self._warm_call_active = False
        self._warm_call_finished = asyncio.Event()
        # Provider/task settings are baked into the resident voice host's
        # environment. If they change during a call, keep that call alive and
        # replace the host as soon as it finishes.
        self._restart_voice_host_after_call = False
        self._resident_host_environment_signature: tuple[tuple[str, str], ...] | None = None
        self._resident_host_ready = False
        self._resident_host_reported_config: dict[str, Any] = {}
        self._receptionist_task: asyncio.Task[None] | None = None
        self._shutting_down = False
        self.receptionist_state = "disabled"
        self._child_reported_error = False
        self._edge_voice_cache: list[dict[str, object]] = []
        self._edge_voice_cache_source = ""
        self._edge_voice_cache_at = 0.0
        self._edge_voice_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._control_events: deque[dict[str, Any]] = deque(maxlen=500)
        self._control_event_sequence = 0
        self._control_activation_lock = asyncio.Lock()
        self.call_state = "IDLE"
        self.current_phone_number = ""
        self.current_public_destination = ""
        self._call_started_at: float | None = None
        self._campaign_worker_id = f"studio-{secrets.token_hex(8)}"
        self._active_campaign_member_id = ""
        self._campaign_original_task_id = ""
        self._campaign_original_channel = ""
        self._gpu_status: dict[str, Any] = {"status": "initializing", "models": {}}
        self._gpu_lock = asyncio.Lock()
        self._setup_routes()

    def _setup_routes(self) -> None:
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/ws", self.handle_websocket)
        self.app.router.add_get("/api/status", self.handle_get_status)
        self.app.router.add_get("/api/config", self.handle_get_config)
        self.app.router.add_get("/api/tts/edge-voices", self.handle_get_edge_voices)
        self.app.router.add_get("/api/llm/models", self.handle_get_llm_models)
        self.app.router.add_get("/api/gpu/status", self.handle_get_gpu_status)
        self.app.router.add_post("/api/gpu/prewarm", self.handle_post_gpu_prewarm)
        self.app.router.add_post("/api/config", self.handle_post_config)
        self.app.router.add_post("/api/remote-link", self.handle_post_remote_link)
        self.app.router.add_post("/api/pairing", self.handle_post_pairing)
        self.app.router.add_post("/api/call/dial", self.handle_post_dial)
        self.app.router.add_post("/api/call/hangup", self.handle_post_hangup)
        self.app.router.add_get("/api/approvals", self.handle_get_approvals)
        self.app.router.add_post("/api/approvals/decide", self.handle_post_approval_decision)
        self.app.router.add_get("/api/mcp/status", self.handle_get_mcp_status)
        self.app.router.add_get("/api/mcp/capabilities", self.handle_get_mcp_capabilities)
        self.app.router.add_get("/api/mcp/identity", self.handle_get_mcp_identity)
        self.app.router.add_post("/api/mcp/dial/request", self.handle_post_mcp_dial_request)
        self.app.router.add_post("/api/mcp/dial/execute", self.handle_post_mcp_dial_execute)
        self.app.router.add_get("/api/control/schema", self.handle_get_control_schema)
        self.app.router.add_get("/api/control/package", self.handle_get_control_package)
        self.app.router.add_get(
            "/api/control/deployments", self.handle_get_control_deployments
        )
        self.app.router.add_get("/api/control/events", self.handle_get_control_events)
        self.app.router.add_post("/api/control/validate", self.handle_post_control_validate)
        self.app.router.add_post("/api/control/stage", self.handle_post_control_stage)
        self.app.router.add_post("/api/control/activate", self.handle_post_control_activate)
        self.app.router.add_post("/api/control/rollback", self.handle_post_control_rollback)
        self.app.router.add_post("/api/control/dial", self.handle_post_control_dial)
        self.app.router.add_post("/api/control/hangup", self.handle_post_control_hangup)
        self.app.router.add_get("/api/persona", self.handle_get_persona)
        self.app.router.add_post("/api/persona", self.handle_post_persona)
        self.app.router.add_get("/api/identity", self.handle_get_identity)
        self.app.router.add_post("/api/identity/revisions", self.handle_post_identity_revision)
        self.app.router.add_post(
            "/api/identity/revisions/evaluate", self.handle_post_identity_evaluate
        )
        self.app.router.add_post(
            "/api/identity/revisions/approve", self.handle_post_identity_approve
        )
        self.app.router.add_post(
            "/api/identity/revisions/activate", self.handle_post_identity_activate
        )
        self.app.router.add_post(
            "/api/identity/revisions/rollback", self.handle_post_identity_rollback
        )
        self.app.router.add_post(
            "/api/identity/history/restore", self.handle_post_identity_history_restore
        )
        self.app.router.add_post(
            "/api/identity/memory-blocks", self.handle_post_identity_memory_block
        )
        self.app.router.add_post(
            "/api/identity/memory-proposals/decide",
            self.handle_post_identity_memory_decision,
        )
        self.app.router.add_post(
            "/api/identity/skills/trust", self.handle_post_identity_skill_trust
        )
        self.app.router.add_post("/api/identity/skills", self.handle_post_identity_skill)
        self.app.router.add_get("/api/memory", self.handle_get_memory)
        self.app.router.add_get("/api/tasks", self.handle_get_tasks)
        self.app.router.add_post("/api/tasks", self.handle_post_task)
        self.app.router.add_post("/api/tasks/delete", self.handle_delete_task)
        self.app.router.add_get("/api/tools", self.handle_get_tools)
        self.app.router.add_post("/api/tools", self.handle_post_tools)
        self.app.router.add_post("/api/tools/test", self.handle_post_tool_test)
        self.app.router.add_get("/api/tools/approvals", self.handle_get_tool_approvals)
        self.app.router.add_post(
            "/api/tools/approvals/decide", self.handle_post_tool_approval_decision
        )
        self.app.router.add_get("/api/openwa", self.handle_get_openwa)
        self.app.router.add_post("/api/openwa", self.handle_post_openwa)
        self.app.router.add_post("/api/openwa/test", self.handle_post_openwa_test)
        self.app.router.add_post("/api/openwa/sessions", self.handle_post_openwa_sessions)
        self.app.router.add_post("/api/openwa/provision", self.handle_post_openwa_provision)
        self.app.router.add_get("/api/web-research", self.handle_get_web_research)
        self.app.router.add_post("/api/web-research", self.handle_post_web_research)
        self.app.router.add_post("/api/web-research/test", self.handle_post_web_research_test)
        self.app.router.add_get("/api/frappe", self.handle_get_frappe)
        self.app.router.add_post("/api/frappe", self.handle_post_frappe)
        self.app.router.add_post("/api/frappe/test", self.handle_post_frappe_test)
        self.app.router.add_get("/api/eval", self.handle_get_eval)
        self.app.router.add_get("/api/product/status", self.handle_get_product_status)
        self.app.router.add_get("/api/channel/status", self.handle_get_channel_status)
        self.app.router.add_post("/api/channel/whatsapp/pair", self.handle_post_whatsapp_pair)
        self.app.router.add_post("/api/product/research", self.handle_post_product_research)
        if STATIC_DIR.exists():
            self.app.router.add_static("/static/", path=STATIC_DIR, name="static")

    async def handle_index(self, request: web.Request) -> web.StreamResponse:
        index_file = STATIC_DIR / "index.html"
        if not index_file.exists():
            return web.Response(text="PhoneAgent Studio is missing its interface.", status=500)
        return web.FileResponse(index_file)

    async def handle_get_persona(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "persona": self.persona_compiler.persona_data,
                "examples": self.persona_compiler.behavioral_examples,
                # The effective behaviour is the shipped defaults merged with
                # any persona override, which is what the call actually uses.
                # Serving only the override would show an empty editor.
                "human_conversation": self.persona_compiler.human_conversation,
            }
        )

    async def handle_get_identity(self, request: web.Request) -> web.Response:
        try:
            state = await asyncio.to_thread(self.identity_kernel.public_state)
        except Exception:
            logger.exception("Identity state could not be loaded")
            return web.json_response(
                {"status": "error", "message": "Identity state is unavailable"}, status=500
            )
        return web.json_response({"status": "ok", **state})

    async def handle_post_identity_revision(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"profile", "reason"}:
                raise ValueError("profile and reason are required")
            if not isinstance(data["profile"], dict):
                raise ValueError("profile must be an object")
            revision = await asyncio.to_thread(
                self.identity_kernel.create_revision,
                data["profile"],
                reason=str(data["reason"]),
                actor="studio_operator",
            )
        except (ValueError, TypeError, IdentityStoreError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        await self.broadcast(
            {"type": "identity_revision_created", "revision_id": revision.revision_id}
        )
        return web.json_response(
            {"status": "ok", "revision": revision.model_dump(mode="json")}, status=201
        )

    async def handle_post_identity_evaluate(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"revision_id"}:
                raise ValueError("revision_id is required")
            revision = await asyncio.to_thread(
                self.identity_kernel.evaluate_revision, str(data["revision_id"])
            )
        except (ValueError, TypeError, IdentityStoreError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        await self.broadcast(
            {
                "type": "identity_revision_evaluated",
                "revision_id": revision.revision_id,
                "passed": bool(revision.evaluation and revision.evaluation.passed),
            }
        )
        return web.json_response({"status": "ok", "revision": revision.model_dump(mode="json")})

    async def handle_post_identity_approve(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"revision_id"}:
                raise ValueError("revision_id is required")
            revision = await asyncio.to_thread(
                self.identity_kernel.approve_revision,
                str(data["revision_id"]),
                actor="studio_operator",
            )
        except (ValueError, TypeError, IdentityStoreError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        await self.broadcast(
            {"type": "identity_revision_approved", "revision_id": revision.revision_id}
        )
        return web.json_response({"status": "ok", "revision": revision.model_dump(mode="json")})

    async def handle_post_identity_activate(self, request: web.Request) -> web.Response:
        if self._dial_in_progress():
            return web.json_response(
                {
                    "status": "error",
                    "message": "Identity activation is blocked while a call is in progress.",
                },
                status=409,
            )
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"revision_id"}:
                raise ValueError("revision_id is required")
            profile = await asyncio.to_thread(
                self.identity_kernel.activate_revision, str(data["revision_id"])
            )
        except (ValueError, TypeError, IdentityStoreError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        await self.broadcast(
            {
                "type": "identity_activated",
                "identity_id": profile.identity_id,
                "version": profile.version,
            }
        )
        return web.json_response({"status": "ok", "active": profile.model_dump(mode="json")})

    async def handle_post_identity_rollback(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"history_file"}:
                raise ValueError("history_file is required")
            revision = await asyncio.to_thread(
                self.identity_kernel.create_rollback_revision,
                str(data["history_file"]),
                actor="studio_operator",
            )
        except (ValueError, TypeError, IdentityStoreError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        await self.broadcast(
            {"type": "identity_revision_created", "revision_id": revision.revision_id}
        )
        return web.json_response(
            {"status": "ok", "revision": revision.model_dump(mode="json")}, status=201
        )

    async def handle_post_identity_history_restore(self, request: web.Request) -> web.Response:
        if self._dial_in_progress():
            return web.json_response(
                {
                    "status": "error",
                    "message": "Identity restore is blocked while a call is in progress.",
                },
                status=409,
            )
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"history_file"}:
                raise ValueError("history_file is required")
            profile = await asyncio.to_thread(
                self.identity_kernel.restore_history,
                str(data["history_file"]),
                actor="studio_operator",
            )
        except (ValueError, TypeError, IdentityStoreError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        await self.broadcast(
            {
                "type": "identity_activated",
                "identity_id": profile.identity_id,
                "version": profile.version,
                "restored_from": str(data["history_file"]),
            }
        )
        return web.json_response(
            {
                "status": "ok",
                "message": f"Archived identity version {profile.version} activated exactly.",
                "active": profile.model_dump(mode="json"),
                "profile_hash": self.identity_kernel.profile_hash,
            }
        )

    async def handle_post_identity_memory_block(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"block"}:
                raise ValueError("block is required")
            block = MemoryBlock.model_validate(data["block"])
            updated = await asyncio.to_thread(
                self.identity_kernel.store.replace_mutable_block,
                block,
                actor="studio_operator",
            )
        except (ValueError, TypeError, IdentityStoreError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        await self.broadcast({"type": "identity_memory_updated", "block_id": updated.block_id})
        return web.json_response({"status": "ok", "block": updated.model_dump(mode="json")})

    async def handle_post_identity_memory_decision(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"proposal_id", "approved"}:
                raise ValueError("proposal_id and approved are required")
            if not isinstance(data["approved"], bool):
                raise ValueError("approved must be boolean")
            proposal = await asyncio.to_thread(
                self.identity_kernel.store.decide_memory_proposal,
                str(data["proposal_id"]),
                approved=data["approved"],
                actor="studio_operator",
            )
        except (ValueError, TypeError, IdentityStoreError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        return web.json_response({"status": "ok", "proposal": proposal.model_dump(mode="json")})

    async def handle_post_identity_skill_trust(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"name", "digest"}:
                raise ValueError("name and digest are required")
            skills, _ = self.identity_kernel.registry.discover()
            skill = skills.get(str(data["name"]))
            if skill is None or skill.source != "user" or skill.digest != str(data["digest"]):
                raise ValueError("exact discovered user skill digest is required")
            await asyncio.to_thread(
                self.identity_kernel.registry.trust_skill,
                skill.name,
                skill.digest,
                actor="studio_operator",
            )
        except (ValueError, TypeError, SkillError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        await self.broadcast({"type": "identity_skill_trusted", "name": skill.name})
        return web.json_response({"status": "ok", "name": skill.name})

    async def handle_post_identity_skill(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            draft = SkillDraft.model_validate(data)
            skill = await asyncio.to_thread(self.identity_kernel.registry.save_user_skill, draft)
        except (ValueError, TypeError, SkillError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        await self.broadcast(
            {"type": "identity_skill_saved", "name": skill.name, "trusted": skill.trusted}
        )
        return web.json_response(
            {"status": "ok", "skill": skill.model_dump(mode="json")}, status=201
        )

    async def handle_post_persona(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("persona must be a JSON object")
            persona = self.persona_compiler.update_persona(data)
            # Recompile so the next call and the editor both see the merge.
            self.persona_compiler.human_conversation = (
                self.persona_compiler._load_human_conversation()
            )
        except (ValueError, TypeError, OSError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        await self.broadcast({"type": "persona_updated", "persona": persona})
        return web.json_response(
            {
                "status": "ok",
                "message": (
                    "Legacy behavior saved for the next call. Active Identity Kernel "
                    "name, role, mission and hard boundaries were not changed."
                ),
                "persona": persona,
            }
        )

    async def handle_post_whatsapp_pair(self, request: web.Request) -> web.Response:
        """Link a WhatsApp account, showing the code the operator must enter.

        The code is useless without the phone it is typed into, so surfacing it
        here does not weaken the account: it only saves a trip to a terminal.
        """

        from .whatsapp_link import WhatsAppLinkError, pair_phone

        try:
            data = await request.json()
            number = str(data.get("phone_number", "")).strip()
            if not number:
                raise ValueError("a WhatsApp phone number is required")
            country = str(data.get("country_code") or self.config.whatsapp_country_code).strip()

            async def announce(code: str) -> None:
                await self.broadcast({"type": "whatsapp_pairing_code", "code": code})

            result = await pair_phone(number, country_code=country, on_code=announce)
        except (WhatsAppLinkError, ValueError, TypeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("WhatsApp pairing failed")
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

        await self.broadcast({"type": "whatsapp_paired", "paired": result["paired"]})
        return web.json_response({"status": "ok", **result})

    async def handle_get_channel_status(self, request: web.Request) -> web.Response:
        """Which call channels can actually place a call right now.

        A channel that cannot dial is shown and explained rather than hidden, so
        an unpaired WhatsApp reads as a missing step and not a missing feature.
        """

        from .whatsapp_link import is_paired, resolve_binary

        try:
            resolve_binary()
            installed = True
        except Exception:
            installed = False
        paired = await is_paired() if installed else False
        whatsapp_on_phone = (await asyncio.to_thread(self._whatsapp_on_phone_error)) is None
        if paired:
            reason = ""
        elif installed:
            reason = "not paired — enter your WhatsApp number below to link this machine"
        else:
            reason = "caller not built — run: cd whatsapp_channel/rust_caller && ./build.sh"
        return web.json_response(
            {
                "active": self.config.call_channel,
                "channels": {
                    "gsm": {
                        "label": "Phone — cellular (GSM)",
                        "available": True,
                        "reason": "",
                    },
                    "whatsapp_phone": {
                        "label": "WhatsApp — placed by the phone (two-way)",
                        "available": whatsapp_on_phone,
                        "reason": (
                            ""
                            if whatsapp_on_phone
                            else "install WhatsApp on the connected phone and sign in"
                        ),
                    },
                    "whatsapp": {
                        "label": "WhatsApp — direct Rust media (two-way)",
                        "available": paired,
                        "reason": reason,
                    },
                },
            }
        )

    async def handle_get_product_status(self, request: web.Request) -> web.Response:
        """Tell the Studio whether product research is available at all."""

        from .tasks.product_pipeline import (
            ENGINE_DIR_ENV,
            available_extraction_models,
            engine_available,
            engine_dir,
        )

        providers = await asyncio.to_thread(available_extraction_models)
        return web.json_response(
            {
                "available": engine_available(),
                "engine_dir": str(engine_dir()),
                "env_var": ENGINE_DIR_ENV,
                "providers": providers,
            }
        )

    async def handle_post_product_research(self, request: web.Request) -> web.Response:
        """Crawl a product site and compile a verified task contract from it."""

        from .tasks.product_pipeline import (
            ProductPipelineError,
            build_task_from_url,
            report_payload,
        )

        try:
            data = await request.json()
            url = str(data.get("url", "")).strip()
            task_id = str(data.get("task_id", "")).strip()
            if not url or not task_id:
                raise ValueError("a product URL and a task id are both required")
            report, activated = await build_task_from_url(
                url,
                task_id=task_id,
                agent_name=str(data.get("agent_name") or "Adam").strip(),
                max_pages=max(1, min(int(data.get("max_pages") or 25), 60)),
                provider=str(data.get("provider") or "auto").strip(),
                model=(str(data.get("model")).strip() or None) if data.get("model") else None,
                activate_when_clean=bool(data.get("activate")),
                strict=bool(data.get("strict")),
                engine=self.task_engine,
                progress=lambda line: self.broadcast({"type": "product_progress", "message": line}),
            )
        except (ProductPipelineError, ValueError, TypeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("Product research failed")
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

        payload = report_payload(report, activated)
        if activated:
            # Writing the contract is not enough: research produced a task the
            # agent never used because it was never selected, so a call still
            # ran on the previous hand-written one.
            self.task_id = report.task_id
            await asyncio.to_thread(self._persist_settings)
            payload["selected"] = True
            await self.broadcast({"type": "task_saved", "task_id": report.task_id})
            await self.broadcast(
                {
                    "type": "call_notice",
                    "message": f"Task '{report.task_id}' is now active for the next call.",
                }
            )
        await self.broadcast({"type": "product_report", **payload})
        return web.json_response({"status": "ok", **payload})

    async def handle_get_memory(self, request: web.Request) -> web.Response:
        await asyncio.to_thread(self.memory_manager.refresh)
        return web.json_response({"callers": self.memory_manager.get_all_callers()})

    async def handle_get_tasks(self, request: web.Request) -> web.Response:
        contracts = self.task_engine.get_all_contracts()
        return web.json_response(
            {
                "tasks": contracts,
                "active_task": self.task_id,
                # Shipped contracts can be edited into a Studio copy but never
                # deleted, so the UI needs to know which is which.
                "user_authored": [
                    contract["id"]
                    for contract in contracts
                    if self.task_engine.is_user_authored(contract["id"])
                ],
            }
        )

    async def handle_post_task(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            contract = await asyncio.to_thread(self.task_engine.save_contract, data)
        except (ValueError, TypeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        await self.broadcast({"type": "task_saved", "task_id": contract["id"]})
        return web.json_response(
            {
                "status": "ok",
                "message": f"Task '{contract['id']}' saved. Select it to use on the next call.",
                "task": contract,
            }
        )

    async def handle_delete_task(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            task_id = str(data.get("task_id", "")).strip()
            if task_id == self.task_id:
                raise ValueError("cannot delete the task that is currently selected")
            await asyncio.to_thread(self.task_engine.delete_contract, task_id)
        except (ValueError, TypeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        await self.broadcast({"type": "task_deleted", "task_id": task_id})
        return web.json_response({"status": "ok", "message": f"Task '{task_id}' deleted."})

    async def handle_get_tools(self, request: web.Request) -> web.Response:
        try:
            state = await asyncio.to_thread(self.tool_control_store.public_state)
            approvals = await asyncio.to_thread(self.tool_approval_queue.list_active)
        except (ToolControlError, OSError) as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)}, status=500
            )
        return web.json_response(
            {
                "status": "ok",
                "config": state,
                "approvals": approvals,
                "live_call": self.call_state not in {"IDLE", "DISCONNECTED"},
                "hot_reload": True,
                "supported_connection_kinds": ["http", "mcp_stdio", "mcp_http"],
            }
        )

    async def handle_post_tools(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"config"}:
                raise ValueError("config is required")
            if not isinstance(data["config"], dict):
                raise ValueError("tool config must be an object")
            saved = await asyncio.to_thread(self.tool_control_store.save, data["config"])
        except (ValueError, TypeError, ToolControlError, OSError, json.JSONDecodeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        active_tools = sorted(
            tool.exposed_name
            for connection in saved.connections
            if connection.enabled
            for tool in connection.tools
            if tool.enabled
        )
        try:
            await asyncio.to_thread(
                self.audit_ledger.append,
                "tool_control_updated",
                {
                    "revision": saved.revision,
                    "connection_ids": [connection.id for connection in saved.connections],
                    "active_tools": active_tools,
                    "live_call": self.call_state not in {"IDLE", "DISCONNECTED"},
                },
            )
        except Exception as exc:
            logger.exception("tool control audit failed")
            return web.json_response(
                {
                    "status": "error",
                    "message": f"Tool configuration was saved but audit failed: {exc}",
                },
                status=500,
            )
        event = {
            "type": "tool_control_updated",
            "revision": saved.revision,
            "active_tools": active_tools,
            "live": self.call_state not in {"IDLE", "DISCONNECTED"},
        }
        await self.broadcast(event)
        return web.json_response(
            {
                "status": "ok",
                "message": (
                    "Tools saved. The live Realtime agent will reload them within one second."
                    if event["live"]
                    else "Tools saved and ready for the next call."
                ),
                "config": self.tool_control_store.public_state(),
                "active_tools": active_tools,
            }
        )

    async def handle_post_tool_test(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) - {"connection", "arguments"}:
                raise ValueError("connection and optional arguments are required")
            if not isinstance(data.get("connection"), dict):
                raise ValueError("connection must be an object")
            arguments = data.get("arguments")
            if arguments is not None and not isinstance(arguments, dict):
                raise ValueError("test arguments must be an object")
            connection = await asyncio.to_thread(
                self.tool_control_store.hydrate_connection, data["connection"]
            )
            discovered, result = await asyncio.wait_for(
                test_connection(connection, arguments=arguments),
                timeout=max(35, connection.timeout_ms / 1_000 + 5),
            )
        except (
            ValueError,
            TypeError,
            ToolControlError,
            McpBrokerError,
            OSError,
            json.JSONDecodeError,
            TimeoutError,
        ) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        public_connection = discovered.model_dump(mode="json")
        public_connection["headers"] = {
            name: "••••••••" for name in public_connection.get("headers", {})
        }
        return web.json_response(
            {
                "status": "ok",
                "message": (
                    f"Connected and discovered {len(discovered.tools)} MCP tools."
                    if discovered.kind != "http"
                    else "HTTP tool connection succeeded."
                ),
                "connection": public_connection,
                "result": result,
            }
        )

    async def handle_get_tool_approvals(self, request: web.Request) -> web.Response:
        try:
            approvals = await asyncio.to_thread(self.tool_approval_queue.list_active)
        except (ToolControlError, OSError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=500)
        return web.json_response({"status": "ok", "approvals": approvals})

    async def handle_post_tool_approval_decision(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"request_id", "approved"}:
                raise ValueError("request_id and approved are required")
            if not isinstance(data["approved"], bool):
                raise ValueError("approved must be true or false")
            record = await asyncio.to_thread(
                self.tool_approval_queue.decide,
                str(data["request_id"]),
                approved=data["approved"],
            )
            await asyncio.to_thread(
                self.audit_ledger.append,
                "tool_approval_decided",
                {
                    "request_id": record["request_id"],
                    "tool_name": record["tool_name"],
                    "approved": data["approved"],
                    "call_id_hash": record["call_id_hash"],
                },
            )
        except (ValueError, TypeError, ToolControlError, OSError, json.JSONDecodeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        await self.broadcast(
            {
                "type": "tool_approval_decided",
                "request_id": record["request_id"],
                "tool_name": record["tool_name"],
                "approved": data["approved"],
            }
        )
        return web.json_response({"status": "ok", "state": record["state"]})

    async def handle_get_openwa(self, request: web.Request) -> web.Response:
        try:
            config = await asyncio.to_thread(self.openwa_config_store.load)
            public = await asyncio.to_thread(self.openwa_config_store.public_state)
            connectivity: dict[str, Any] = {"reachable": False, "session_ready": False}
            async with aiohttp.ClientSession() as session:
                client = OpenWAClient(config, session)
                try:
                    connectivity["health"] = await client.health()
                    connectivity["reachable"] = True
                except Exception as exc:
                    connectivity["message"] = str(exc)
                if connectivity["reachable"] and config.api_key and config.session_id:
                    try:
                        status = await client.session_status()
                        connectivity["session"] = _public_openwa_session(status)
                        connectivity["session_ready"] = _openwa_session_ready(status)
                    except Exception as exc:
                        connectivity["session_message"] = str(exc)
        except (OpenWAError, OSError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=500)
        return web.json_response(
            {
                "status": "ok",
                "config": public,
                "connectivity": connectivity,
                "dashboard_url": config.base_url,
                "hot_reload": True,
            }
        )

    async def handle_post_openwa(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"config"}:
                raise ValueError("config is required")
            if not isinstance(data["config"], dict):
                raise ValueError("OpenWA config must be an object")
            saved = await asyncio.to_thread(self.openwa_config_store.save, data["config"])
            await asyncio.to_thread(
                self.audit_ledger.append,
                "openwa_config_updated",
                {
                    "revision": saved.revision,
                    "enabled": saved.enabled,
                    "session_configured": bool(saved.session_id),
                    "live_events_enabled": saved.live_events_enabled,
                    "active_tools": [tool.name for tool in saved.tools if tool.enabled],
                },
            )
        except (ValueError, TypeError, OpenWAError, OSError, json.JSONDecodeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        await self.broadcast(
            {
                "type": "openwa_config_updated",
                "revision": saved.revision,
                "enabled": saved.enabled,
            }
        )
        return web.json_response(
            {
                "status": "ok",
                "message": (
                    "OpenWA saved. The active Realtime call will reload it within one second."
                    if self.call_state not in {"IDLE", "DISCONNECTED"}
                    else "OpenWA saved for the next call."
                ),
                "config": self.openwa_config_store.public_state(),
            }
        )

    async def handle_post_openwa_test(self, request: web.Request) -> web.Response:
        try:
            try:
                data = await request.json()
            except Exception:
                data = {}
            if isinstance(data, dict) and "config" in data and isinstance(data["config"], dict):
                config = await asyncio.to_thread(self.openwa_config_store.hydrate, data["config"])
            else:
                config = await asyncio.to_thread(self.openwa_config_store.load)
            async with aiohttp.ClientSession() as session:
                client = OpenWAClient(config, session)
                health = await client.health()
                session_status = (
                    await client.session_status()
                    if config.api_key and config.session_id
                    else None
                )
        except (ValueError, TypeError, OpenWAError, OSError, json.JSONDecodeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        session_ready = _openwa_session_ready(session_status)
        if session_status is not None and not session_ready:
            state = str(session_status.get("status") or "not ready")
            return web.json_response(
                {
                    "status": "not_ready",
                    "message": (
                        f"OpenWA is reachable, but the selected WhatsApp session is {state}. "
                        "Open the OpenWA dashboard and pair or reconnect the phone."
                    ),
                    "health": _sanitize_openwa(health),
                    "session": _public_openwa_session(session_status),
                },
                status=409,
            )
        return web.json_response(
            {
                "status": "ok",
                "message": (
                    "OpenWA and the selected WhatsApp session are reachable."
                    if session_status is not None
                    else "OpenWA server is reachable. Configure a session and key next."
                ),
                "health": _sanitize_openwa(health),
                "session": _public_openwa_session(session_status),
            }
        )

    async def handle_post_openwa_sessions(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"base_url", "admin_key"}:
                raise ValueError("base_url and admin_key are required")
            admin_key = str(data["admin_key"]).strip()
            if not admin_key:
                raise ValueError("admin_key is required")
            draft = self.openwa_config_store.load().model_copy(
                update={"base_url": str(data["base_url"]).strip(), "enabled": False}
            )
            draft = type(draft).model_validate(draft.model_dump(mode="json"))
            async with aiohttp.ClientSession() as session:
                sessions = await OpenWAClient(draft, session).list_sessions(admin_key)
        except (ValueError, TypeError, OpenWAError, OSError, json.JSONDecodeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        return web.json_response(
            {"status": "ok", "sessions": [_public_openwa_session(item) for item in sessions]}
        )

    async def handle_post_openwa_provision(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            required = {"base_url", "admin_key", "session_id"}
            if not isinstance(data, dict) or set(data) != required:
                raise ValueError("base_url, admin_key and session_id are required")
            admin_key = str(data["admin_key"]).strip()
            session_id = str(data["session_id"]).strip()
            if not admin_key or not session_id:
                raise ValueError("admin key and session id are required")
            current = self.openwa_config_store.load()
            draft = current.model_copy(
                update={
                    "base_url": str(data["base_url"]).strip(),
                    "session_id": session_id,
                    "enabled": False,
                }
            )
            draft = type(draft).model_validate(draft.model_dump(mode="json"))
            async with aiohttp.ClientSession() as session:
                client = OpenWAClient(draft, session)
                await client.session_status(api_key=admin_key)
                provisioned = await client.provision_operator_key(admin_key, session_id)
            payload = draft.model_dump(mode="json")
            payload["api_key"] = str(provisioned["apiKey"])
            await asyncio.to_thread(self.openwa_config_store.save, payload)
            await asyncio.to_thread(
                self.audit_ledger.append,
                "openwa_key_provisioned",
                {
                    "session_id_hash": hashlib.sha256(session_id.encode()).hexdigest()[:16],
                    "key_id": str(provisioned.get("id") or "")[:80],
                },
            )
        except (ValueError, TypeError, OpenWAError, OSError, json.JSONDecodeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        return web.json_response(
            {
                "status": "ok",
                "message": "Dedicated session-scoped PhoneAgent key created and stored privately.",
                "config": self.openwa_config_store.public_state(),
                "key_prefix": str(provisioned.get("keyPrefix") or ""),
            }
        )

    async def handle_get_web_research(self, request: web.Request) -> web.Response:
        try:
            config = await asyncio.to_thread(self.web_research_config_store.load)
            public = await asyncio.to_thread(self.web_research_config_store.public_state)
            async with aiohttp.ClientSession() as session:
                engine = WebResearchEngine(config, session)
                crawl4ai = await engine.crawl4ai_health()
        except (WebResearchError, OSError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=500)
        return web.json_response(
            {
                "status": "ok",
                "config": public,
                "connectivity": {"crawl4ai": crawl4ai},
                "hot_reload": True,
            }
        )

    async def handle_post_web_research(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"config"}:
                raise ValueError("config is required")
            if not isinstance(data["config"], dict):
                raise ValueError("web research config must be an object")
            saved = await asyncio.to_thread(
                self.web_research_config_store.save, data["config"]
            )
            await asyncio.to_thread(
                self.audit_ledger.append,
                "web_research_config_updated",
                {
                    "revision": saved.revision,
                    "enabled": saved.enabled,
                    "search_results": saved.search_results,
                    "pages_to_read": saved.pages_to_read,
                    "crawl4ai_enabled": saved.crawl4ai_enabled,
                    "task_ids": saved.task_ids,
                },
            )
        except (ValueError, TypeError, WebResearchError, OSError, json.JSONDecodeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        await self.broadcast(
            {
                "type": "web_research_config_updated",
                "revision": saved.revision,
                "enabled": saved.enabled,
            }
        )
        return web.json_response(
            {
                "status": "ok",
                "message": (
                    "Web research saved. The active Realtime call will reload it within one second."
                    if self.call_state not in {"IDLE", "DISCONNECTED"}
                    else "Web research saved for the next call."
                ),
                "config": self.web_research_config_store.public_state(),
            }
        )

    async def handle_post_web_research_test(self, request: web.Request) -> web.Response:
        try:
            try:
                data = await request.json()
            except Exception:
                data = {}
            if isinstance(data, dict) and "config" in data and isinstance(data["config"], dict):
                config = await asyncio.to_thread(
                    self.web_research_config_store.hydrate, data["config"]
                )
            else:
                config = await asyncio.to_thread(self.web_research_config_store.load)
            query = str(data.get("query") or "test connectivity")
            language = str(data.get("language") or "auto")
            async with aiohttp.ClientSession() as session:
                engine = WebResearchEngine(config, session)
                result = await engine.research(query, language)
                crawl4ai = await engine.crawl4ai_health()
            await asyncio.to_thread(
                self.audit_ledger.append,
                "web_research_connection_tested",
                {
                    "query_hash": hashlib.sha256(query.encode()).hexdigest()[:16],
                    "confidence": result["confidence"],
                    "source_count": result["read_sources"],
                    "elapsed_ms": result["elapsed_ms"],
                },
            )
        except (
            ValueError,
            TypeError,
            WebResearchError,
            OSError,
            json.JSONDecodeError,
            aiohttp.ClientError,
        ) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        public_result = {
            **result,
            "sources": [
                {
                    **{key: value for key, value in source.items() if key != "content"},
                    "content_preview": str(source.get("content") or "")[:1_000],
                    "content_chars": len(str(source.get("content") or "")),
                }
                for source in result["sources"]
            ],
        }
        return web.json_response(
            {
                "status": "ok",
                "message": (
                    f"Research succeeded with {result['read_sources']} source(s) in "
                    f"{result['elapsed_ms']} ms."
                ),
                "result": public_result,
                "connectivity": {"crawl4ai": crawl4ai},
            }
        )

    async def handle_get_frappe(self, request: web.Request) -> web.Response:
        try:
            config = await asyncio.to_thread(self.frappe_config_store.load)
            public = await asyncio.to_thread(self.frappe_config_store.public_state)
            connectivity: dict[str, Any] = {"reachable": False, "required_ready": False}
            if config.api_key and config.api_secret:
                async with aiohttp.ClientSession() as session:
                    health = await FrappeClient(config, session).health()
                connectivity.update(
                    {
                        "reachable": True,
                        "required_ready": bool(health.get("required_ready")),
                        "health": health,
                    }
                )
        except (FrappeIntegrationError, OSError, aiohttp.ClientError) as exc:
            connectivity = {"reachable": False, "required_ready": False, "message": str(exc)}
            try:
                public = await asyncio.to_thread(self.frappe_config_store.public_state)
                config = await asyncio.to_thread(self.frappe_config_store.load)
            except Exception:
                return web.json_response(
                    {"status": "error", "message": str(exc)}, status=500
                )
        return web.json_response(
            {
                "status": "ok",
                "config": public,
                "connectivity": connectivity,
                "urls": {
                    "erp": f"{config.base_url}/login?redirect-to=/app",
                    "crm": f"{config.base_url}/login?redirect-to=/crm",
                    "helpdesk": f"{config.base_url}/login?redirect-to=/helpdesk",
                },
                "hot_reload": True,
            }
        )

    async def handle_post_frappe(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"config"}:
                raise ValueError("config is required")
            if not isinstance(data["config"], dict):
                raise ValueError("Frappe config must be an object")
            saved = await asyncio.to_thread(self.frappe_config_store.save, data["config"])
            await asyncio.to_thread(
                self.audit_ledger.append,
                "frappe_config_updated",
                {
                    "revision": saved.revision,
                    "enabled": saved.enabled,
                    "campaign_autopilot_enabled": saved.campaign_autopilot_enabled,
                    "active_tools": [tool.name for tool in saved.tools if tool.enabled],
                },
            )
        except (
            ValueError,
            TypeError,
            FrappeIntegrationError,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        await self.broadcast(
            {
                "type": "frappe_config_updated",
                "revision": saved.revision,
                "enabled": saved.enabled,
                "campaign_autopilot_enabled": saved.campaign_autopilot_enabled,
            }
        )
        return web.json_response(
            {
                "status": "ok",
                "message": (
                    "Business Suite saved. The active Realtime call will reload it "
                    "within one second."
                    if self.call_state not in {"IDLE", "DISCONNECTED"}
                    else "Business Suite saved for autonomous calls."
                ),
                "config": self.frappe_config_store.public_state(),
            }
        )

    async def handle_post_frappe_test(self, request: web.Request) -> web.Response:
        try:
            try:
                data = await request.json()
            except Exception:
                data = {}
            if isinstance(data, dict) and "config" in data and isinstance(data["config"], dict):
                config = await asyncio.to_thread(self.frappe_config_store.hydrate, data["config"])
            else:
                config = await asyncio.to_thread(self.frappe_config_store.load)
            async with aiohttp.ClientSession() as session:
                health = await FrappeClient(config, session).health()
            await asyncio.to_thread(
                self.audit_ledger.append,
                "frappe_connection_tested",
                {"site": config.site_name, "required_ready": health.get("required_ready")},
            )
        except (
            ValueError,
            TypeError,
            FrappeIntegrationError,
            OSError,
            json.JSONDecodeError,
            aiohttp.ClientError,
        ) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        return web.json_response(
            {
                "status": "ok",
                "message": "ERPNext, CRM, Helpdesk and the PhoneAgent business API are reachable.",
                "health": health,
            }
        )

    async def handle_get_eval(self, request: web.Request) -> web.Response:
        await asyncio.to_thread(self.memory_manager.refresh)
        return web.json_response(
            {"evaluations": self.memory_manager.get_recent_evaluations(limit=20)}
        )

    async def handle_get_status(self, request: web.Request) -> web.Response:
        identity = self.identity_kernel.active
        identity_status = self.identity_kernel.production_status()
        return web.json_response(
            {
                "status": "ok",
                "call_state": self.call_state,
                "phone_number": self.current_public_destination,
                "clients_connected": len(self._ws_clients),
                "remote_link": self.remote_link_status(),
                "inbound_receptionist": {
                    "enabled": self.auto_answer_enabled,
                    "state": self.receptionist_state,
                },
                "voice_host": {
                    "ready": self._resident_host_ready,
                    "configuration_current": self._resident_host_matches_current_config(),
                    "effective_config": {
                        key: value
                        for key, value in self._resident_host_reported_config.items()
                        if key != "system_prompt_sha256"
                    },
                },
                "identity": {
                    "identity_id": identity.identity_id,
                    "version": identity.version,
                    "profile_hash": self.identity_kernel.profile_hash,
                    "evaluation_passed": identity_status["evaluation_passed"],
                    "production_ready": identity_status["ready"],
                },
            }
        )

    def _public_config(self) -> dict[str, Any]:
        return {
            "tts_provider": self.config.tts_provider,
            "tts_model": self.config.tts_model,
            "tts_voice_id": self.config.tts_voice_id,
            "llm_provider": self.config.llm_provider,
            "llm_model": self.config.llm_model,
            "stt_provider": self.config.stt_provider,
            "stt_model": self.config.stt_model,
            "stt_language": self.config.stt_language,
            "tts_aggregation": self.config.tts_aggregation,
            "google_tts_scene": self.config.google_tts_scene,
            "google_tts_sample_context": self.config.google_tts_sample_context,
            "speculative_pipeline_enabled": self.config.speculative_pipeline_enabled,
            "conversational_reflex_enabled": self.config.conversational_reflex_enabled,
            "auto_answer_enabled": self.auto_answer_enabled,
            "auto_answer_state": self.receptionist_state,
            "pipeline_mode": self.config.pipeline_mode,
            "call_channel": self.config.call_channel,
            "whatsapp_country_code": self.config.whatsapp_country_code,
            "chatgpt_realtime_voice": self.config.chatgpt_realtime_voice,
            "chatgpt_realtime_model": self.config.chatgpt_realtime_model,
            "chatgpt_realtime_transport": self.config.chatgpt_realtime_transport,
            "chatgpt_realtime_reasoning_effort": (self.config.chatgpt_realtime_reasoning_effort),
            "chatgpt_realtime_transcription_model": (
                self.config.chatgpt_realtime_transcription_model
            ),
            "chatgpt_realtime_input_languages": list(self.config.chatgpt_realtime_input_languages),
            "chatgpt_realtime_noise_reduction": (self.config.chatgpt_realtime_noise_reduction),
            "chatgpt_realtime_vad_eagerness": self.config.chatgpt_realtime_vad_eagerness,
            "chatgpt_realtime_vad_mode": self.config.chatgpt_realtime_vad_mode,
            "chatgpt_realtime_vad_silence_ms": self.config.chatgpt_realtime_vad_silence_ms,
            "chatgpt_realtime_idle_timeout_ms": self.config.chatgpt_realtime_idle_timeout_ms,
            "chatgpt_realtime_speed": self.config.chatgpt_realtime_speed,
            "task_id": self.task_id,
            "system_prompt": self.system_prompt,
        }

    def _load_saved_settings(self) -> None:
        if not self.settings_path.exists():
            return
        try:
            harden_private_file(self.settings_path)
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            updates = {
                key: str(value).strip()
                for key, value in data.items()
                if key in self.CONFIG_FIELDS and str(value).strip()
            }
            updates.update(
                {
                    key: bool(value)
                    for key, value in data.items()
                    if key in self.BOOLEAN_CONFIG_FIELDS and isinstance(value, bool)
                }
            )
            updates.update(
                {key: str(data[key]).strip() for key in self.GOOGLE_TTS_TEXT_FIELDS if key in data}
            )
            candidate = replace(self.config, **updates)
            candidate.validate(require_credentials=False)
            task_id = str(data.get("task_id", self.task_id)).strip()
            self.task_engine.require_contract(task_id)
            self.config = candidate
            self.task_id = task_id
            self.system_prompt = str(data.get("system_prompt", self.system_prompt)).strip()
            if isinstance(data.get("auto_answer_enabled"), bool):
                self.auto_answer_enabled = data["auto_answer_enabled"]
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring invalid saved Studio settings: %s", exc)

    def _persist_settings(self) -> None:
        atomic_write_private(
            self.settings_path,
            json.dumps(self._public_config(), ensure_ascii=False, indent=2) + "\n",
        )

    async def handle_get_config(self, request: web.Request) -> web.Response:
        return web.json_response(self._public_config())

    async def handle_get_edge_voices(self, request: web.Request) -> web.Response:
        force_refresh = request.query.get("refresh") == "1"
        now = time.monotonic()
        cache_seconds = (
            EDGE_VOICE_CACHE_SECONDS
            if self._edge_voice_cache_source == "live"
            else EDGE_VOICE_FALLBACK_RETRY_SECONDS
        )
        if (
            not force_refresh
            and self._edge_voice_cache
            and now - self._edge_voice_cache_at < cache_seconds
        ):
            return web.json_response(
                {
                    "status": "ok",
                    "source": self._edge_voice_cache_source,
                    "voices": self._edge_voice_cache,
                }
            )

        async with self._edge_voice_lock:
            try:
                voices = await asyncio.wait_for(fetch_edge_voice_catalog(), timeout=8.0)
                source = "live"
            except Exception as exc:
                logger.warning("Could not refresh Edge voice catalog; using fallback: %s", exc)
                voices = fallback_edge_voice_catalog()
                source = "fallback"
            self._edge_voice_cache = voices
            self._edge_voice_cache_source = source
            self._edge_voice_cache_at = time.monotonic()

        return web.json_response({"status": "ok", "source": source, "voices": voices})

    async def handle_get_llm_models(self, request: web.Request) -> web.Response:
        """Fetch available models for a chosen LLM provider, dynamically querying local Ollama if selected."""
        provider = request.query.get("provider", self.config.llm_provider).strip().lower()
        if provider == "ollama":
            base_url = (self.config.ollama_base_url or "http://127.0.0.1:11434").rstrip("/")
            try:
                timeout = aiohttp.ClientTimeout(total=4.0)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(f"{base_url}/api/tags") as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            raw_models = data.get("models", [])
                            models = [
                                {
                                    "name": m.get("name"),
                                    "size": m.get("size", 0),
                                    "details": m.get("details", {}),
                                    "capabilities": m.get("capabilities", []),
                                }
                                for m in raw_models
                                if m.get("name")
                            ]
                            names = [m["name"] for m in models]
                            return web.json_response(
                                {
                                    "status": "ok",
                                    "provider": "ollama",
                                    "available": bool(names),
                                    "models": models,
                                    "names": names,
                                    "error": "" if names else "Ollama is running, but no models are installed.",
                                }
                            )
                        return web.json_response(
                            {
                                "status": "ok",
                                "provider": "ollama",
                                "available": False,
                                "models": [],
                                "names": [],
                                "error": f"Ollama returned HTTP {resp.status}",
                            }
                        )
            except Exception as exc:
                return web.json_response(
                    {
                        "status": "ok",
                        "provider": "ollama",
                        "available": False,
                        "models": [],
                        "names": [],
                        "error": f"Ollama is not reachable on {base_url} ({exc})",
                    }
                )
        elif provider == "antigravity_gemini":
            names = ["gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"]
            return web.json_response(
                {
                    "status": "ok",
                    "provider": "antigravity_gemini",
                    "available": True,
                    "models": [{"name": n} for n in names],
                    "names": names,
                    "error": "",
                }
            )
        elif provider == "gemini":
            names = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"]
            return web.json_response(
                {
                    "status": "ok",
                    "provider": "gemini",
                    "available": bool(self.config.google_api_key),
                    "models": [{"name": n} for n in names],
                    "names": names,
                    "error": "" if self.config.google_api_key else "Missing Google Gemini API key.",
                }
            )
        elif provider == "openai":
            names = ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1", "o3-mini"]
            return web.json_response(
                {
                    "status": "ok",
                    "provider": "openai",
                    "available": bool(self.config.openai_api_key),
                    "models": [{"name": n} for n in names],
                    "names": names,
                    "error": "" if self.config.openai_api_key else "Missing OpenAI API key.",
                }
            )
        elif provider == "openrouter":
            names = [
                "meta-llama/llama-3.3-70b-instruct",
                "google/gemini-2.5-flash",
                "deepseek/deepseek-chat",
                "openai/gpt-4o-mini",
            ]
            return web.json_response(
                {
                    "status": "ok",
                    "provider": "openrouter",
                    "available": bool(self.config.openrouter_api_key),
                    "models": [{"name": n} for n in names],
                    "names": names,
                    "error": "" if self.config.openrouter_api_key else "Missing OpenRouter API key.",
                }
            )
        elif provider == "vllm":
            base_url = (self.config.vllm_base_url or "http://127.0.0.1:8000/v1").rstrip("/")
            try:
                timeout = aiohttp.ClientTimeout(total=3.0)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(f"{base_url}/models") as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            names = [m.get("id") for m in data.get("data", []) if m.get("id")]
                            if names:
                                return web.json_response(
                                    {
                                        "status": "ok",
                                        "provider": "vllm",
                                        "available": True,
                                        "models": [{"name": n} for n in names],
                                        "names": names,
                                        "error": "",
                                    }
                                )
            except Exception:
                pass
            names = [
                "Qwen/Qwen3.8-27B-AWQ",
                "Qwen/Qwen2.5-32B-Instruct-AWQ",
                "Qwen/Qwen2.5-14B-Instruct-AWQ",
                "Qwen/Qwen2.5-7B-Instruct",
            ]
            return web.json_response(
                {
                    "status": "ok",
                    "provider": "vllm",
                    "available": True,
                    "models": [{"name": n} for n in names],
                    "names": names,
                    "error": "",
                }
            )
        elif provider == "lmstudio":
            base_url = (self.config.lmstudio_base_url or "http://127.0.0.1:1234/v1").rstrip("/")
            try:
                timeout = aiohttp.ClientTimeout(total=3.0)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(f"{base_url}/models") as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            names = [m.get("id") for m in data.get("data", []) if m.get("id")]
                            if names:
                                return web.json_response(
                                    {
                                        "status": "ok",
                                        "provider": "lmstudio",
                                        "available": True,
                                        "models": [{"name": n} for n in names],
                                        "names": names,
                                        "error": "",
                                    }
                                )
            except Exception:
                pass
            names = ["local-model"]
            return web.json_response(
                {
                    "status": "ok",
                    "provider": "lmstudio",
                    "available": True,
                    "models": [{"name": n} for n in names],
                    "names": names,
                    "error": "",
                }
            )
        else:
            return web.json_response(
                {
                    "status": "error",
                    "message": f"Unknown provider: {provider}",
                },
                status=400,
            )

    async def handle_get_gpu_status(self, request: web.Request) -> web.Response:
        """Return the current resident GPU models and memory state."""
        return web.json_response(
            {
                "status": "ok",
                "gpu": self._gpu_status,
            }
        )

    async def handle_post_gpu_prewarm(self, request: web.Request) -> web.Response:
        """Trigger an immediate background GPU prewarm of Kokoro TTS and Ollama phi4/qwen."""
        async with self._gpu_lock:
            self._gpu_status = {"status": "prewarming", "models": {}}
            self._spawn_background_task(self._prewarm_gpu_models_task())
        return web.json_response(
            {
                "status": "ok",
                "message": "GPU prewarming started in the background for the configured models.",
                "gpu": self._gpu_status,
            }
        )

    async def _prewarm_gpu_models_task(self) -> None:
        """Background worker to make the configured speech models GPU-resident.

        Only meaningful when there is no warm voice host. Call audio is served by
        the ``phone_voice_agent`` child, which has its own CUDA context and loads
        its own copy of SenseVoice and Kokoro; preloading them here as well put a
        second 1.85 GB of identical weights on the card and warmed a process that
        never touches a call.
        """

        if _environment_bool("PHONE_AGENT_WARM_VOICE_HOST", True):
            self._gpu_status = {
                "status": "ready",
                "models": {
                    "owner": "warm voice host",
                    "note": (
                        "Speech models are resident in the phone_voice_agent child that "
                        "serves call audio. Loading them here as well would duplicate "
                        "them in a process that never touches a call."
                    ),
                },
                "timestamp": time.time(),
            }
            logger.info(
                "GPU Model Preloader: skipped; the warm voice host owns the speech models"
            )
            await self.broadcast({"type": "gpu_status_updated", "gpu": self._gpu_status})
            return
        try:
            from .production_pipeline import prewarm_gpu_resident_models

            logger.info("GPU Model Preloader: prewarming the configured speech models...")
            # No model list: the prewarm derives what to load from the active
            # configuration. Naming models here pinned phi4 (9.05 GB) and
            # qwen2.5:3b (1.93 GB) with keep_alive=-1 regardless of provider.
            results = await prewarm_gpu_resident_models(self.config)
            self._gpu_status = {
                "status": "ready",
                "models": results,
                "timestamp": time.time(),
            }
            logger.info("GPU Model Preloader: All models resident in VRAM: %s", results)
            await self.broadcast({"type": "gpu_status_updated", "gpu": self._gpu_status})
        except Exception as exc:
            self._gpu_status = {"status": "error", "error": str(exc), "models": {}}
            logger.warning("GPU Model Preloader warning: %s", exc)

    async def handle_post_config(self, request: web.Request) -> web.Response:
        old_host_environment = self._child_environment(
            auto_answer=self.auto_answer_enabled,
            call_channel="gsm",
            command_stdin=True,
        )
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("configuration must be a JSON object")
            updates = {
                key: str(value).strip()
                for key, value in data.items()
                if key in self.CONFIG_FIELDS and str(value).strip()
            }
            for key in self.BOOLEAN_CONFIG_FIELDS:
                if key in data:
                    if not isinstance(data[key], bool):
                        raise ValueError(f"{key} must be true or false")
                    updates[key] = data[key]
            for key in self.GOOGLE_TTS_TEXT_FIELDS:
                if key in data:
                    updates[key] = str(data[key]).strip()
            # The Studio selects an STT engine, not an independent model name.
            # Keeping the previous provider's model made the persisted config
            # claim combinations such as whisper_turbo + SenseVoiceSmall.
            if "stt_provider" in updates and "stt_model" not in updates:
                updates["stt_model"] = {
                    "sensevoice": "iic/SenseVoiceSmall",
                    "sensevoice_small": "iic/SenseVoiceSmall",
                    "whisper_turbo": "large-v3-turbo",
                    "whisper_cuda": "large-v3-turbo",
                    "whisper_local": "large-v3-turbo",
                    "whisper_mlx": "large-v3-turbo",
                    "distil_whisper": "distil-large-v3",
                    "parakeet_local": "mlx-community/parakeet-tdt-0.6b-v3",
                }.get(str(updates["stt_provider"]), self.config.stt_model)
            effective_stt_provider = str(
                updates.get("stt_provider", self.config.stt_provider)
            )
            effective_stt_language = str(
                updates.get("stt_language", self.config.stt_language)
            )
            if effective_stt_provider in {"sensevoice", "sensevoice_small"} and not (
                effective_stt_language.lower().startswith("en")
            ):
                updates["stt_provider"] = "whisper_turbo"
                updates["stt_model"] = "large-v3-turbo"
            candidate = replace(self.config, **updates)
            candidate.validate(require_credentials=False)
            task_id = str(data.get("task_id", self.task_id)).strip()
            self.task_engine.require_contract(task_id)
            system_prompt = str(data.get("system_prompt", self.system_prompt)).strip()
            auto_answer_enabled = data.get(
                "auto_answer_enabled", self.auto_answer_enabled
            )
            if not isinstance(auto_answer_enabled, bool):
                raise ValueError("auto_answer_enabled must be true or false")
        except (ValueError, TypeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        self.config = candidate
        self.task_id = task_id
        self.system_prompt = system_prompt
        self.auto_answer_enabled = auto_answer_enabled
        new_host_environment = self._child_environment(
            auto_answer=self.auto_answer_enabled,
            call_channel="gsm",
            command_stdin=True,
        )
        voice_host_changed = old_host_environment != new_host_environment
        try:
            await asyncio.to_thread(self._persist_settings)
        except OSError as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=500)
        await self.broadcast({"type": "config_updated", "config": self._public_config()})
        if voice_host_changed:
            # Every provider/task setting is baked into the resident child's
            # environment, not only auto-answer. Reusing a host after changing
            # STT previously made the Studio show SenseVoice while the next
            # call still ran the old Whisper process. Never terminate an active
            # call; replace its host immediately afterwards instead.
            if self.call_state == "IDLE" and not self._warm_call_active:
                await self._stop_inbound_monitor()
                await self._start_inbound_monitor()
            else:
                self._restart_voice_host_after_call = True
        return web.json_response(
            {
                "status": "ok",
                "message": (
                    "Settings saved. The voice host will restart after the current call."
                    if self._restart_voice_host_after_call
                    else "Settings applied. The voice host is warming for the next call."
                ),
                "config": self._public_config(),
            }
        )

    async def handle_post_remote_link(self, request: web.Request) -> web.Response:
        """Let Studio own the cable-free link instead of an environment variable."""

        # Cross-origin and DNS-rebinding are already refused by
        # local_security_middleware, so this only validates the payload.
        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "message": "invalid JSON"}, status=400
            )
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return web.json_response(
                {"status": "error", "message": "enabled must be true or false"}, status=400
            )
        port = data.get("port")
        if port is not None and not isinstance(port, int):
            return web.json_response(
                {"status": "error", "message": "port must be a number"}, status=400
            )
        try:
            status = await self.set_remote_link(enabled=enabled, port=port)
        except (ValueError, RuntimeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        return web.json_response({"status": "ok", "remote_link": status})

    async def handle_post_pairing(self, request: web.Request) -> web.Response:
        """Render pairing material the handset can scan in one pass.

        The key, the address and the port travel together because a phone that
        is correctly keyed but pointed at the wrong host fails exactly as
        silently as a mismatched key.
        """

        try:
            data = await request.json()
        except Exception:
            data = {}
        rotate = bool(data.get("rotate", False))
        addresses = local_addresses()
        host = str(data.get("host", "")).strip() or (addresses[0] if addresses else "")
        if not host:
            return web.json_response(
                {"status": "error", "message": "no reachable address for this machine"},
                status=400,
            )
        port = int(data.get("port", self._remote_link_settings.listen_port))
        try:
            payload = await asyncio.to_thread(build_pairing, host, port, rotate=rotate)
            svg = await asyncio.to_thread(payload.to_qr_svg)
        except Exception as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        if rotate:
            # Rotating breaks the USB path until the phone is paired again, so
            # it is worth an audit entry rather than a silent change.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self.audit_ledger.append,
                    "link_key_rotated",
                    {"fingerprint": key_fingerprint(payload.key)},
                )
            # The relay loads the key once, at construction. Without this it
            # keeps verifying against the old one and rejects the very handset
            # that just scanned the new code -- which looks like a broken scan
            # rather than a stale process.
            await self._restart_remote_link_for_new_key()
        return web.json_response(
            {
                "status": "ok",
                "qr_svg": svg,
                "fingerprint": key_fingerprint(payload.key),
                "host": host,
                "port": port,
                "addresses": addresses,
            }
        )

    async def handle_post_dial(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("request must be a JSON object")
            phone_number = str(data.get("phone_number", "")).strip()
            operator_approved = data.get("operator_approved", False)
            recording_consent = data.get("recording_consent", False)
            if not isinstance(operator_approved, bool) or not isinstance(recording_consent, bool):
                raise ValueError("approval and recording consent must be boolean")
        except (ValueError, TypeError):
            phone_number = ""
            operator_approved = False
            recording_consent = False
        if not phone_number:
            return web.json_response(
                {"status": "error", "message": "phone_number is required"}, status=400
            )
        return await self._begin_dial(
            phone_number,
            operator_approved=operator_approved,
            recording_consent=recording_consent,
        )

    async def _begin_dial(
        self,
        phone_number: str,
        *,
        operator_approved: bool,
        recording_consent: bool,
    ) -> web.Response:
        identity_status = self.identity_kernel.production_status()
        if not identity_status["ready"]:
            return web.json_response(
                {
                    "status": "error",
                    "message": (
                        "Active identity is not production-ready. Run the required evaluation, "
                        "approve its exact hash, and activate it before dialing."
                    ),
                    "identity": identity_status,
                },
                status=503,
            )
        if self._dial_in_progress():
            return web.json_response(
                {"status": "error", "message": "A call is already in progress."}, status=409
            )
        if self.call_state != "IDLE":
            return web.json_response(
                {
                    "status": "error",
                    "message": "An incoming call is already in progress.",
                },
                status=409,
            )
        # A task whose child already exited must not block the next call.
        await self._cancel_dial_task()
        decision = self.call_policy.decide_dial(
            phone_number,
            approved=operator_approved,
            country_code=self.config.whatsapp_country_code,
            reserve=False,
        )
        if not decision.allowed:
            try:
                await asyncio.to_thread(
                    self.audit_ledger.append,
                    "dial_denied",
                    {
                        "destination": decision.public_destination or "invalid",
                        "channel": self.config.call_channel,
                        "reason": decision.reason,
                    },
                )
            except Exception:
                logger.exception("could not append denied-dial audit event")
            return web.json_response({"status": "error", "message": decision.reason}, status=403)
        preflight_error = await self._gateway_preflight()
        if preflight_error:
            await self.set_call_state("IDLE")
            await self.broadcast({"type": "call_error", "message": preflight_error})
            return web.json_response({"status": "error", "message": preflight_error}, status=503)
        # Reserve rate/cooldown capacity only once the selected channel is
        # genuinely ready. Failed hardware preflights must not consume quota.
        decision = self.call_policy.decide_dial(
            phone_number,
            approved=operator_approved,
            country_code=self.config.whatsapp_country_code,
            reserve=True,
        )
        if not decision.allowed:
            return web.json_response({"status": "error", "message": decision.reason}, status=403)
        try:
            await asyncio.to_thread(
                self.audit_ledger.append,
                "dial_allowed",
                {
                    "destination": decision.public_destination,
                    "channel": self.config.call_channel,
                    "recording_consent": recording_consent,
                    "task_id": self.task_id,
                },
            )
        except Exception:
            logger.exception("could not append allowed-dial audit event")
            return web.json_response(
                {
                    "status": "error",
                    "message": "The security audit ledger is unavailable; dialing was refused.",
                },
                status=503,
            )
        phone_number = decision.normalized or phone_number
        self.current_phone_number = phone_number
        self.current_public_destination = decision.public_destination or ""
        self._call_started_at = time.monotonic()
        await self.set_call_state("DIALING")
        self._dial_task = asyncio.create_task(
            self._execute_dial(phone_number, recording_consent=recording_consent)
        )
        return web.json_response({"status": "ok", "message": "Dialing…"})

    def _mcp_authorized(self, request: web.Request) -> bool:
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {self._control_token}"
        return hmac.compare_digest(supplied, expected)

    def _runtime_control(self) -> RuntimeControl:
        values = {
            name: getattr(self.config, name)
            for name in RuntimeControl.model_fields
            if name not in {"auto_answer_enabled", "system_prompt"}
        }
        values["chatgpt_realtime_input_languages"] = list(
            self.config.chatgpt_realtime_input_languages
        )
        values["auto_answer_enabled"] = self.auto_answer_enabled
        values["system_prompt"] = self.system_prompt
        return RuntimeControl.model_validate(values)

    def _current_agent_package(self) -> AgentPackage:
        active_deployment = self.control_plane_store.active()
        identity = self.identity_kernel.active
        task = self.task_engine.require_contract(self.task_id)
        skills, _ = self.identity_kernel.registry.discover()
        user_skill_drafts = [
            {
                "name": skill.name,
                "description": skill.description,
                "version": skill.version,
                "instructions": skill.instructions,
                "allowed_tools": skill.allowed_tools,
                "mcp_tools": skill.mcp_tools,
                "task_ids": skill.task_ids,
                "languages": [language.value for language in skill.languages],
                "priority": skill.priority,
            }
            for skill in skills.values()
            if skill.source == "user" and skill.name in identity.enabled_skills
        ]
        return AgentPackage(
            package_id=(
                active_deployment.package.package_id
                if active_deployment is not None
                else "current_agent"
            ),
            display_name=(
                active_deployment.package.display_name
                if active_deployment is not None
                else f"{identity.core.name} · {task['title']}"
            ),
            objective=(
                active_deployment.package.objective
                if active_deployment is not None
                else str(task["objective"])
            ),
            identity=identity,
            task=task,
            runtime=self._runtime_control(),
            skills=user_skill_drafts,
            memory_blocks=[
                block for block in self.identity_kernel.store.load_blocks() if block.mutable
            ],
            tools=self.tool_control_store.public_state(),
            openwa=self.openwa_config_store.public_state(),
            web_research=self.web_research_config_store.public_state(),
            business=self.frappe_config_store.public_state(),
            labels=(active_deployment.package.labels if active_deployment is not None else {}),
        )

    def _effective_control_state_hash(self) -> str:
        return state_hash(self._current_agent_package().model_dump(mode="json"))

    def _runtime_candidate(self, runtime: RuntimeControl) -> ProviderConfig:
        values = runtime.model_dump(
            exclude={"auto_answer_enabled", "system_prompt"}, mode="python"
        )
        values["chatgpt_realtime_input_languages"] = tuple(
            values["chatgpt_realtime_input_languages"]
        )
        candidate = replace(self.config, **values)
        candidate.validate(require_credentials=False)
        return candidate

    def _validate_agent_package(self, package: AgentPackage) -> PackageValidation:
        checks: list[dict[str, Any]] = []
        warnings: list[str] = []

        task = self.task_engine.validate_contract(package.task)
        checks.append({"id": "task.schema", "passed": True, "task_id": task["id"]})
        self._runtime_candidate(package.runtime)
        checks.append({"id": "runtime.compatibility", "passed": True})
        tools = self.tool_control_store.hydrate(package.tools)
        openwa = self.openwa_config_store.hydrate(package.openwa)
        research = self.web_research_config_store.hydrate(package.web_research)
        business = self.frappe_config_store.hydrate(package.business)
        checks.extend(
            [
                {"id": "tools.schema", "passed": True},
                {"id": "openwa.schema", "passed": True},
                {"id": "web_research.schema", "passed": True},
                {"id": "business.schema", "passed": True},
            ]
        )
        if any(block.source.value == "agent_inferred" for block in package.memory_blocks):
            raise ValueError("agent-inferred memory must use the memory proposal workflow")
        checks.append({"id": "memory.mutable_only", "passed": True})

        skills, _ = self.identity_kernel.registry.discover()
        available_skills = {name for name, skill in skills.items() if skill.trusted}
        available_skills.update(skill.name for skill in package.skills)
        identity_report = self.identity_kernel.evaluator.evaluate(
            package.identity,
            available_skills=available_skills,
        )
        checks.append(
            {
                "id": "identity.contract",
                "passed": identity_report.passed,
                "score": identity_report.score,
                "critical_findings": [
                    finding.check_id
                    for finding in identity_report.findings
                    if not finding.passed and finding.severity == "critical"
                ],
            }
        )
        warnings.extend(
            finding.message
            for finding in identity_report.findings
            if not finding.passed and finding.severity != "critical"
        )

        enabled_tools = {
            tool.exposed_name
            for connection in tools.connections
            if connection.enabled
            for tool in connection.tools
            if tool.enabled
        }
        if openwa.enabled:
            enabled_tools.update(tool.name for tool in openwa.tools if tool.enabled)
        if research.enabled:
            enabled_tools.add("web_research")
        if business.enabled:
            enabled_tools.update(tool.name for tool in business.tools if tool.enabled)
        load_user_tools()
        enabled_tools.update(registered_tools())
        requested = set(task.get("allowed_tools", []))
        built_in = {
            "end_call",
            "load_agent_skill",
            "knowledge_base_search",
            "callback_schedule",
            "send_checkout_link",
        }
        unavailable = sorted(requested - enabled_tools - built_in)
        if unavailable:
            warnings.append(
                "Task allows tools that are not active in this package: "
                + ", ".join(unavailable)
            )
        checks.append(
            {
                "id": "task.tool_bindings",
                "passed": not unavailable,
                "unavailable": unavailable,
            }
        )
        valid = identity_report.passed
        payload = package.model_dump(mode="json")
        return PackageValidation(
            valid=valid,
            package_hash=package_hash(package),
            effective_state_hash=state_hash(payload),
            checks=checks,
            warnings=warnings,
        )

    @staticmethod
    def _snapshot_private_files(paths: list[Path]) -> dict[Path, bytes | None]:
        return {path: path.read_bytes() if path.exists() else None for path in paths}

    @staticmethod
    def _restore_private_files(snapshot: dict[Path, bytes | None]) -> None:
        for path, payload in snapshot.items():
            if payload is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_private(path, payload)

    async def _activate_control_deployment(self, deployment_id: str) -> Any:
        if self._dial_in_progress():
            raise ControlPlaneError("AgentPackage activation is blocked during a call")
        async with self._control_activation_lock:
            record = self.control_plane_store.load(deployment_id)
            if record.state != "staged":
                raise ControlPlaneError("only a staged deployment can activate")
            current_hash = self._effective_control_state_hash()
            if record.base_state_hash != current_hash:
                raise ControlPlaneError(
                    "PhoneAgent configuration changed after staging; validate and stage again"
                )
            validation = self._validate_agent_package(record.package)
            if not validation.valid or validation.package_hash != record.package_hash:
                raise ControlPlaneError("staged AgentPackage no longer passes exact validation")
            self.control_plane_store.mark_activating(deployment_id)
            package = record.package
            task_path = self.task_engine.user_contracts_dir / f"{package.task['id']}.yaml"
            skill_paths = [
                self.identity_kernel.registry.user_root / skill.name / "SKILL.md"
                for skill in package.skills
            ]
            paths = [
                self.settings_path,
                task_path,
                self.tool_control_store.path,
                self.openwa_config_store.path,
                self.web_research_config_store.path,
                self.frappe_config_store.path,
                self.identity_kernel.store.active_path,
                self.identity_kernel.store.blocks_path,
                self.identity_kernel.registry.trust_path,
                *skill_paths,
            ]
            snapshot = self._snapshot_private_files(paths)
            previous = (
                self.config,
                self.task_id,
                self.system_prompt,
                self.auto_answer_enabled,
            )
            try:
                candidate = self._runtime_candidate(package.runtime)
                self.task_engine.save_contract(package.task)
                self.tool_control_store.save(package.tools)
                self.openwa_config_store.save(package.openwa)
                self.web_research_config_store.save(package.web_research)
                self.frappe_config_store.save(package.business)
                for skill_draft in package.skills:
                    skill = self.identity_kernel.registry.save_user_skill(skill_draft)
                    self.identity_kernel.registry.trust_skill(
                        skill.name, skill.digest, actor=record.created_by
                    )
                self.identity_kernel.store.replace_all_mutable_blocks(
                    package.memory_blocks, actor=record.created_by
                )
                self.config = candidate
                self.task_id = str(package.task["id"])
                self.system_prompt = package.runtime.system_prompt
                self.auto_answer_enabled = package.runtime.auto_answer_enabled
                self._persist_settings()

                revision = self.identity_kernel.create_revision(
                    package.identity,
                    reason=f"AgentPackage {package.package_id}: {record.reason}",
                    actor=record.created_by,
                )
                revision = self.identity_kernel.evaluate_revision(revision.revision_id)
                if revision.evaluation is None or not revision.evaluation.passed:
                    raise ControlPlaneError("identity contract evaluation failed")
                self.identity_kernel.approve_revision(
                    revision.revision_id, actor=record.created_by
                )
                self.identity_kernel.activate_revision(revision.revision_id)
                active = self.control_plane_store.mark_active(deployment_id)
            except Exception as exc:
                self._restore_private_files(snapshot)
                for skill_path in skill_paths:
                    if snapshot.get(skill_path) is None:
                        try:
                            skill_path.parent.rmdir()
                        except OSError:
                            pass
                self.config, self.task_id, self.system_prompt, self.auto_answer_enabled = previous
                self.task_engine._load_contracts()
                self.control_plane_store.mark_failed(deployment_id, str(exc))
                raise

            if self.auto_answer_enabled != previous[3]:
                await self._stop_inbound_monitor()
                await self._start_inbound_monitor()
            await asyncio.to_thread(
                self.audit_ledger.append,
                "agent_package_activated",
                {
                    "deployment_id": active.deployment_id,
                    "package_id": package.package_id,
                    "package_hash": active.package_hash,
                    "actor": active.created_by,
                },
            )
            await self.broadcast(
                {
                    "type": "agent_package_activated",
                    "deployment_id": active.deployment_id,
                    "package_id": package.package_id,
                    "package_hash": active.package_hash,
                }
            )
            return active

    async def handle_get_control_schema(self, request: web.Request) -> web.Response:
        if not self._mcp_authorized(request):
            return web.json_response({"status": "error", "message": "unauthorized"}, status=401)
        return web.json_response(
            {
                "status": "ok",
                "schema": AgentPackage.model_json_schema(),
                "deployment_states": ["staged", "activating", "active", "superseded", "failed"],
                "immutable_boundaries": [
                    "gsm_media",
                    "whatsapp_media",
                    "android_privileged_routing",
                    "caller_binding",
                    "secret_redaction",
                    "audit_integrity",
                    "one_call_lock",
                ],
            }
        )

    async def handle_get_control_package(self, request: web.Request) -> web.Response:
        if not self._mcp_authorized(request):
            return web.json_response({"status": "error", "message": "unauthorized"}, status=401)
        package = self._current_agent_package()
        active = self.control_plane_store.active()
        return web.json_response(
            {
                "status": "ok",
                "package": package.model_dump(mode="json"),
                "effective_state_hash": self._effective_control_state_hash(),
                "active_deployment_id": active.deployment_id if active else None,
            }
        )

    async def handle_get_control_deployments(self, request: web.Request) -> web.Response:
        if not self._mcp_authorized(request):
            return web.json_response({"status": "error", "message": "unauthorized"}, status=401)
        return web.json_response(
            {
                "status": "ok",
                "deployments": [
                    {
                        "deployment_id": item.deployment_id,
                        "state": item.state,
                        "package_id": item.package.package_id,
                        "display_name": item.package.display_name,
                        "package_hash": item.package_hash,
                        "base_state_hash": item.base_state_hash,
                        "valid": item.validation.valid,
                        "warning_count": len(item.validation.warnings),
                        "reason": item.reason,
                        "created_by": item.created_by,
                        "created_at": item.created_at,
                        "activated_at": item.activated_at,
                        "failure": item.failure,
                    }
                    for item in self.control_plane_store.list(limit=50)
                ],
            }
        )

    async def handle_get_control_events(self, request: web.Request) -> web.Response:
        if not self._mcp_authorized(request):
            return web.json_response({"status": "error", "message": "unauthorized"}, status=401)
        try:
            after = max(0, int(request.query.get("after", "0")))
            limit = min(200, max(1, int(request.query.get("limit", "100"))))
        except ValueError:
            return web.json_response(
                {"status": "error", "message": "after and limit must be integers"}, status=400
            )
        events = [item for item in self._control_events if item["sequence"] > after][:limit]
        return web.json_response(
            {
                "status": "ok",
                "events": events,
                "next_after": events[-1]["sequence"] if events else after,
            }
        )

    async def handle_post_control_validate(self, request: web.Request) -> web.Response:
        if not self._mcp_authorized(request):
            return web.json_response({"status": "error", "message": "unauthorized"}, status=401)
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"package"}:
                raise ValueError("package is required")
            package = AgentPackage.model_validate(data["package"])
            validation = self._validate_agent_package(package)
        except (ValueError, TypeError, ControlPlaneError, json.JSONDecodeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        return web.json_response(
            {"status": "ok", "validation": validation.model_dump(mode="json")}
        )

    async def handle_post_control_stage(self, request: web.Request) -> web.Response:
        if not self._mcp_authorized(request):
            return web.json_response({"status": "error", "message": "unauthorized"}, status=401)
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"package", "reason", "created_by"}:
                raise ValueError("package, reason and created_by are required")
            package = AgentPackage.model_validate(data["package"])
            validation = self._validate_agent_package(package)
            if not validation.valid:
                raise ValueError("AgentPackage has contract-critical validation failures")
            record = self.control_plane_store.stage(
                package,
                validation,
                base_state_hash=self._effective_control_state_hash(),
                reason=str(data["reason"]),
                actor=str(data["created_by"]),
            )
            await asyncio.to_thread(
                self.audit_ledger.append,
                "agent_package_staged",
                {
                    "deployment_id": record.deployment_id,
                    "package_id": package.package_id,
                    "package_hash": record.package_hash,
                    "actor": record.created_by,
                },
            )
        except (ValueError, TypeError, ControlPlaneError, json.JSONDecodeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        return web.json_response(
            {"status": "ok", "deployment": record.model_dump(mode="json")}, status=201
        )

    async def handle_post_control_activate(self, request: web.Request) -> web.Response:
        if not self._mcp_authorized(request):
            return web.json_response({"status": "error", "message": "unauthorized"}, status=401)
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"deployment_id"}:
                raise ValueError("deployment_id is required")
            active = await self._activate_control_deployment(str(data["deployment_id"]))
        except (ValueError, TypeError, ControlPlaneError, json.JSONDecodeError) as exc:
            status = 409 if "call" in str(exc).lower() or "changed" in str(exc).lower() else 400
            return web.json_response({"status": "error", "message": str(exc)}, status=status)
        except Exception as exc:
            logger.exception("AgentPackage activation failed")
            return web.json_response({"status": "error", "message": str(exc)}, status=500)
        return web.json_response(
            {"status": "ok", "deployment": active.model_dump(mode="json")}
        )

    async def handle_post_control_rollback(self, request: web.Request) -> web.Response:
        if not self._mcp_authorized(request):
            return web.json_response({"status": "error", "message": "unauthorized"}, status=401)
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {
                "deployment_id",
                "reason",
                "created_by",
            }:
                raise ValueError("deployment_id, reason and created_by are required")
            source = self.control_plane_store.load(str(data["deployment_id"]))
            validation = self._validate_agent_package(source.package)
            staged = self.control_plane_store.stage(
                source.package,
                validation,
                base_state_hash=self._effective_control_state_hash(),
                reason=str(data["reason"]),
                actor=str(data["created_by"]),
            )
            active = await self._activate_control_deployment(staged.deployment_id)
        except (ValueError, TypeError, ControlPlaneError, json.JSONDecodeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("AgentPackage rollback failed")
            return web.json_response({"status": "error", "message": str(exc)}, status=500)
        return web.json_response(
            {"status": "ok", "deployment": active.model_dump(mode="json")}
        )

    async def handle_post_control_hangup(self, request: web.Request) -> web.Response:
        if not self._mcp_authorized(request):
            return web.json_response({"status": "error", "message": "unauthorized"}, status=401)
        return await self.handle_post_hangup(request)

    async def handle_post_control_dial(self, request: web.Request) -> web.Response:
        """Dial under the local administrator control token.

        The token is the operator authority; all normal destination policy,
        cooldown, rate, hardware, consent and one-call checks still execute.
        """

        if not self._mcp_authorized(request):
            return web.json_response({"status": "error", "message": "unauthorized"}, status=401)
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {
                "destination",
                "recording_consent",
            }:
                raise ValueError("destination and recording_consent are required")
            destination = str(data["destination"]).strip()
            recording_consent = data["recording_consent"]
            if not isinstance(recording_consent, bool):
                raise ValueError("recording_consent must be true or false")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        return await self._begin_dial(
            destination,
            operator_approved=True,
            recording_consent=recording_consent,
        )

    def _prune_approvals(self) -> None:
        now = time.monotonic()
        for request_id, approval in list(self._approvals.items()):
            if now - approval.created_at > APPROVAL_TTL_SECONDS:
                del self._approvals[request_id]
        while len(self._approvals) > 64:
            oldest = min(self._approvals.values(), key=lambda item: item.created_at)
            del self._approvals[oldest.request_id]

    async def handle_get_mcp_status(self, request: web.Request) -> web.Response:
        if not self._mcp_authorized(request):
            return web.json_response({"status": "error", "message": "unauthorized"}, status=401)
        return web.json_response(
            {
                "status": "ok",
                "call_state": self.call_state,
                "destination": self.current_public_destination,
                "channel": self.config.call_channel,
                "task_id": self.task_id,
                "identity_version": self.identity_kernel.active.version,
                "identity_hash": self.identity_kernel.profile_hash,
            }
        )

    async def handle_get_mcp_capabilities(self, request: web.Request) -> web.Response:
        if not self._mcp_authorized(request):
            return web.json_response({"status": "error", "message": "unauthorized"}, status=401)
        return web.json_response(
            {
                "status": "ok",
                "channels": ["gsm", "whatsapp_phone", "whatsapp"],
                "active_channel": self.config.call_channel,
                "dial_requires_operator_approval": True,
                "administrator_control_token_may_dial": True,
                "recording_requires_per_call_consent": True,
                "maximum_call_duration_seconds": (self.call_policy.config.max_call_duration_secs),
                "identity_kernel": {
                    "versioned_constitution": True,
                    "progressive_skills": True,
                    "reviewed_memory": True,
                    "live_realtime_evaluation": True,
                },
                "control_plane": {
                    "agent_package_schema_version": 1,
                    "atomic_activation": True,
                    "stale_write_protection": True,
                    "rollback": True,
                    "bounded_live_events": True,
                    "framework_code_mutation": False,
                    "media_pipeline_mutation": False,
                },
            }
        )

    async def handle_get_mcp_identity(self, request: web.Request) -> web.Response:
        if not self._mcp_authorized(request):
            return web.json_response({"status": "error", "message": "unauthorized"}, status=401)
        profile = self.identity_kernel.active
        skills = self.identity_kernel.active_skills(
            task_id=self.task_id, language=self.config.stt_language
        )
        production_status = self.identity_kernel.production_status()
        return web.json_response(
            {
                "status": "ok",
                "identity_id": profile.identity_id,
                "version": profile.version,
                "profile_hash": self.identity_kernel.profile_hash,
                "name": profile.core.name,
                "role": profile.core.role,
                "mission": profile.core.mission,
                "values": profile.core.values,
                "decision_priorities": profile.core.decision_priorities,
                "supported_languages": profile.supported_languages,
                "enabled_skills": [skill.name for skill in skills],
                "evaluation_passed": production_status["evaluation_passed"],
                "production_ready": production_status["ready"],
            }
        )

    async def handle_post_mcp_dial_request(self, request: web.Request) -> web.Response:
        if not self._mcp_authorized(request):
            return web.json_response({"status": "error", "message": "unauthorized"}, status=401)
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"destination", "recording_consent"}:
                raise ValueError("request fields are invalid")
            destination = str(data["destination"]).strip()
            recording_consent = data["recording_consent"]
            if not isinstance(recording_consent, bool):
                raise ValueError("recording_consent must be boolean")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        return await self._begin_dial(
            destination,
            operator_approved=True,
            recording_consent=recording_consent,
        )

    async def handle_get_approvals(self, request: web.Request) -> web.Response:
        self._prune_approvals()
        return web.json_response(
            {
                "approvals": [
                    {
                        "request_id": approval.request_id,
                        "destination": approval.public_destination,
                        "recording_consent": approval.recording_consent,
                        "state": approval.state,
                    }
                    for approval in self._approvals.values()
                    if approval.state in {"pending", "approved"}
                ]
            }
        )

    async def handle_post_approval_decision(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"request_id", "approved"}:
                raise ValueError("approval decision fields are invalid")
            request_id = str(data["request_id"])
            approved = data["approved"]
            if not isinstance(approved, bool):
                raise ValueError("approved must be boolean")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        self._prune_approvals()
        approval = self._approvals.get(request_id)
        if approval is None or approval.state != "pending":
            return web.json_response(
                {"status": "error", "message": "approval request is unavailable"}, status=404
            )
        approval.state = "approved" if approved else "rejected"
        await asyncio.to_thread(
            self.audit_ledger.append,
            "approval_decided",
            {
                "request_id": request_id,
                "destination": approval.public_destination,
                "approved": approved,
            },
        )
        await self.broadcast(
            {"type": "approval_decided", "request_id": request_id, "approved": approved}
        )
        return web.json_response({"status": approval.state})

    async def handle_post_mcp_dial_execute(self, request: web.Request) -> web.Response:
        if not self._mcp_authorized(request):
            return web.json_response({"status": "error", "message": "unauthorized"}, status=401)
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"request_id"}:
                raise ValueError("request_id is required")
            request_id = str(data["request_id"])
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        self._prune_approvals()
        approval = self._approvals.get(request_id)
        if approval is None or approval.state != "approved":
            return web.json_response(
                {"status": "error", "message": "exact operator approval is required"},
                status=403,
            )
        approval.state = "executing"
        response = await self._begin_dial(
            approval.destination,
            operator_approved=True,
            recording_consent=approval.recording_consent,
        )
        approval.state = "used" if response.status == 200 else "approved"
        return response

    async def _gateway_preflight(self) -> str | None:
        """Prove the selected channel can actually place a call.

        The cellular checks — ADB, port forwards, the phone's gateway health —
        are specific to that channel. Running them for a WhatsApp call refused
        the dial over an Android phone the call was never going to use.
        """

        if self.config.call_channel == "whatsapp":
            return await self._whatsapp_preflight()
        # whatsapp_phone rides the cellular audio path, so it needs every one of
        # the cellular checks, plus WhatsApp present on the phone.
        cellular = await asyncio.to_thread(self._gateway_preflight_sync)
        if cellular or self.config.call_channel != "whatsapp_phone":
            return cellular
        return await asyncio.to_thread(self._whatsapp_on_phone_error)

    def _whatsapp_on_phone_error(self) -> str | None:
        from .whatsapp_phone_client import WHATSAPP_PACKAGE

        adb = ["adb"]
        device_id = os.getenv("PHONE_AGENT_DEVICE_ID", "").strip()
        if device_id:
            adb.extend(["-s", device_id])
        try:
            listed = subprocess.run(
                [*adb, "shell", "pm", "list", "packages", WHATSAPP_PACKAGE],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"Could not ask the phone about WhatsApp: {exc}"
        if WHATSAPP_PACKAGE not in listed.stdout:
            return (
                "WhatsApp is not installed on the phone. Install it, sign in, and save "
                "the number you want to call in the phone's contacts."
            )
        return None

    async def _whatsapp_preflight(self) -> str | None:
        from .whatsapp_link import is_paired, resolve_binary

        try:
            resolve_binary()
        except Exception as exc:
            return str(exc)
        if not await is_paired():
            return (
                "WhatsApp is not paired. Choose the WhatsApp channel and link this "
                "machine before dialling."
            )
        return None

    def _gateway_preflight_sync(self) -> str | None:
        host = os.getenv("PHONE_AGENT_CONTROL_HOST", "127.0.0.1").strip()
        port = int(os.getenv("PHONE_AGENT_CONTROL_PORT", "8765"))
        # The relay presents the phone on this same loopback, so "local" no
        # longer implies USB. Demanding adb here refused every dial on a
        # perfectly healthy tunnel; the health probe below still proves the
        # handset is actually reachable.
        tunnelled = (
            self._remote_link is not None and self._remote_link.stats.phone_connected
        )
        if host in {"127.0.0.1", "localhost"} and not tunnelled:
            adb = ["adb"]
            device_id = os.getenv("PHONE_AGENT_DEVICE_ID", "").strip()
            if device_id:
                adb.extend(["-s", device_id])
            try:
                state = subprocess.run(
                    [*adb, "get-state"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return "ADB is unavailable. Start ADB and reconnect the Android phone."
            if state.returncode != 0 or state.stdout.strip() != "device":
                return (
                    "Android phone is not connected to ADB. Reconnect USB, unlock the phone, "
                    "and authorize USB debugging."
                )
            for forwarded_port in (port, 8766, 8767, 8768):
                forwarded = subprocess.run(
                    [*adb, "forward", f"tcp:{forwarded_port}", f"tcp:{forwarded_port}"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                if forwarded.returncode != 0:
                    return "Could not establish the Android PhoneAgent USB connection."

        try:
            with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=2) as response:
                health = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError):
            return (
                "Android is connected, but the PhoneAgent gateway is unreachable. "
                "Confirm the PhoneAgent system service is running on the phone."
            )
        if health.get("status") != "ok":
            return "The Android PhoneAgent gateway reported that it is not ready."
        if health.get("dialer_role") is False:
            return "PhoneAgent is not the active Android dialer application."
        return None

    def _child_environment(
        self,
        *,
        recording_consent: bool = False,
        auto_answer: bool = False,
        call_channel: str | None = None,
        command_stdin: bool = False,
    ) -> dict[str, str]:
        env = os.environ.copy()
        if command_stdin:
            env["PHONE_AGENT_COMMAND_STDIN"] = "true"

        env.update(
            {
                "PHONE_AGENT_TTS_PROVIDER": self.config.tts_provider,
                "PHONE_AGENT_TTS_MODEL": self.config.tts_model,
                "PHONE_AGENT_TTS_VOICE": self.config.tts_voice_id,
                "PHONE_AGENT_LLM_PROVIDER": self.config.llm_provider,
                "PHONE_AGENT_LLM_MODEL": self.config.llm_model,
                "PHONE_AGENT_STT_PROVIDER": self.config.stt_provider,
                "PHONE_AGENT_STT_MODEL": self.config.stt_model,
                "PHONE_AGENT_STT_LANGUAGE": self.config.stt_language,
                "PHONE_AGENT_TTS_AGGREGATION": self.config.tts_aggregation,
                "PHONE_AGENT_GOOGLE_TTS_SCENE": self.config.google_tts_scene,
                "PHONE_AGENT_GOOGLE_TTS_SAMPLE_CONTEXT": (self.config.google_tts_sample_context),
                "PHONE_AGENT_SPECULATIVE_PIPELINE": (
                    "true" if self.config.speculative_pipeline_enabled else "false"
                ),
                "PHONE_AGENT_CONVERSATIONAL_REFLEX": (
                    "true" if self.config.conversational_reflex_enabled else "false"
                ),
                "PHONE_AGENT_PIPELINE_MODE": self.config.pipeline_mode,
                "PHONE_AGENT_CALL_CHANNEL": call_channel or self.config.call_channel,
                "PHONE_AGENT_WHATSAPP_COUNTRY": self.config.whatsapp_country_code,
                "PHONE_AGENT_CHATGPT_VOICE": self.config.chatgpt_realtime_voice,
                "PHONE_AGENT_CHATGPT_MODEL": self.config.chatgpt_realtime_model,
                "PHONE_AGENT_CHATGPT_TRANSPORT": self.config.chatgpt_realtime_transport,
                "PHONE_AGENT_CHATGPT_REASONING_EFFORT": (
                    self.config.chatgpt_realtime_reasoning_effort
                ),
                "PHONE_AGENT_CHATGPT_TRANSCRIPTION_MODEL": (
                    self.config.chatgpt_realtime_transcription_model
                ),
                "PHONE_AGENT_CHATGPT_INPUT_LANGUAGES": ",".join(
                    self.config.chatgpt_realtime_input_languages
                ),
                "PHONE_AGENT_CHATGPT_NOISE_REDUCTION": (
                    self.config.chatgpt_realtime_noise_reduction
                ),
                "PHONE_AGENT_CHATGPT_VAD_EAGERNESS": (self.config.chatgpt_realtime_vad_eagerness),
                "PHONE_AGENT_CHATGPT_VAD_MODE": self.config.chatgpt_realtime_vad_mode,
                "PHONE_AGENT_CHATGPT_VAD_THRESHOLD": str(
                    self.config.chatgpt_realtime_vad_threshold
                ),
                "PHONE_AGENT_CHATGPT_VAD_PREFIX_MS": str(
                    self.config.chatgpt_realtime_vad_prefix_ms
                ),
                "PHONE_AGENT_CHATGPT_VAD_SILENCE_MS": str(
                    self.config.chatgpt_realtime_vad_silence_ms
                ),
                "PHONE_AGENT_CHATGPT_IDLE_TIMEOUT_MS": str(
                    self.config.chatgpt_realtime_idle_timeout_ms
                ),
                "PHONE_AGENT_CHATGPT_SPEED": str(self.config.chatgpt_realtime_speed),
                "PHONE_AGENT_TASK_ID": self.task_id,
                "PHONE_AGENT_SYSTEM_PROMPT": self.system_prompt,
                "PHONE_AGENT_EVENT_STREAM": "true",
                "PHONE_AGENT_AUTO_ANSWER": "true" if auto_answer else "false",
                "PHONE_AGENT_PERSONA_PATH": str(self.persona_compiler.persona_path),
                "PHONE_AGENT_MEMORY_PATH": str(self.memory_manager.storage_path),
                "PHONE_AGENT_RECORDING_ENABLED": ("true" if recording_consent else "false"),
                "PHONE_AGENT_RECORDING_CONSENT": ("true" if recording_consent else "false"),
                "PHONE_AGENT_USE_ADB_FORWARD": (
                    "false"
                    if (
                        self._remote_link is not None
                        and self._remote_link.stats.phone_connected
                    )
                    else "true"
                ),
                "PHONE_AGENT_TOOL_CONTROL": str(self.tool_control_store.path),
                "PHONE_AGENT_TOOL_APPROVAL_DIR": str(self.tool_approval_queue.directory),
                "PHONE_AGENT_OPENWA_CONFIG": str(self.openwa_config_store.path),
                "PHONE_AGENT_WEB_RESEARCH_CONFIG": str(
                    self.web_research_config_store.path
                ),
                "PHONE_AGENT_FRAPPE_CONFIG": str(self.frappe_config_store.path),
            }
        )
        return env

    @staticmethod
    def _voice_host_environment_signature(
        environment: dict[str, str],
    ) -> tuple[tuple[str, str], ...]:
        """Identify every environment value that can alter voice-host behavior."""

        return tuple(
            sorted(
                (name, value)
                for name, value in environment.items()
                if name.startswith("PHONE_AGENT_")
                # The remote phone can connect after the host is spawned. That
                # changes only how the next transport opens, not the configured
                # STT/LLM/TTS/task behavior verified by this signature.
                and name != "PHONE_AGENT_USE_ADB_FORWARD"
            )
        )

    def _expected_voice_host_config(self) -> dict[str, Any]:
        return {
            "pipeline_mode": self.config.pipeline_mode,
            "stt_provider": self.config.stt_provider,
            "stt_model": self.config.stt_model,
            "stt_language": self.config.stt_language,
            "llm_provider": self.config.llm_provider,
            "llm_model": self.config.llm_model,
            "tts_provider": self.config.tts_provider,
            "tts_model": self.config.tts_model,
            "tts_voice_id": self.config.tts_voice_id,
            "tts_aggregation": self.config.tts_aggregation,
            "task_id": self.task_id,
            "system_prompt_sha256": hashlib.sha256(
                self.system_prompt.encode("utf-8")
            ).hexdigest(),
            "auto_answer": self.auto_answer_enabled,
        }

    def _expected_resident_host_environment_signature(
        self,
    ) -> tuple[tuple[str, str], ...]:
        environment = self._child_environment(
            auto_answer=self.auto_answer_enabled,
            recording_consent=False,
            call_channel="gsm",
            command_stdin=True,
        )
        return self._voice_host_environment_signature(environment)

    def _resident_host_matches_current_config(self) -> bool:
        return bool(
            self._resident_host_ready
            and self._resident_host_reported_config == self._expected_voice_host_config()
            and self._resident_host_environment_signature
            == self._expected_resident_host_environment_signature()
        )

    async def _set_receptionist_state(self, state: str, message: str = "") -> None:
        self.receptionist_state = state
        await self.broadcast(
            {
                "type": "receptionist_status",
                "enabled": self.auto_answer_enabled,
                "state": state,
                "message": message,
            }
        )

    async def _start_inbound_monitor(self) -> None:
        """Keep one warm voice host alive, whether or not it answers inbound calls.

        The host loads SenseVoice and Kokoro into its own CUDA context, which
        measured 11.3 s and 4.3 s on this machine -- about 20 s before the phone
        could even be dialled. That cost used to be paid on every outbound call,
        because the only warm host was the inbound receptionist and it was gated
        behind the auto-answer toggle. Starting it unconditionally is what makes
        an outbound dial reuse loaded models instead of paying the cold start.

        Whether it *answers* an incoming call is still the operator's choice; that
        is carried by the child's auto_answer environment, not by its existence.
        """

        if self._shutting_down or (
            self._receptionist_task is not None and not self._receptionist_task.done()
        ):
            return
        if not _environment_bool("PHONE_AGENT_WARM_VOICE_HOST", True) and not (
            self.auto_answer_enabled
        ):
            # Spawning a real voice host is a side effect of merely constructing
            # the Studio, so it has to be switchable off for tests and for any
            # embedding that only wants the HTTP surface.
            return
        self._receptionist_task = asyncio.create_task(
            self._inbound_monitor_supervisor(),
            name="phoneagent-inbound-receptionist",
        )

    async def _stop_inbound_monitor(self) -> None:
        task = self._receptionist_task
        self._receptionist_task = None
        if task is not None and not task.done():
            task.cancel()
        process = self._receptionist_process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=8)
            except TimeoutError:
                process.kill()
                await process.wait()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        self._receptionist_process = None
        self._resident_host_environment_signature = None
        self._resident_host_ready = False
        self._resident_host_reported_config = {}
        await self._set_receptionist_state(
            "disabled" if not self.auto_answer_enabled else "paused"
        )

    async def _inbound_monitor_supervisor(self) -> None:
        failures = 0
        try:
            while not self._shutting_down:
                if self._active_process is not None:
                    await asyncio.sleep(0.25)
                    continue
                await self._set_receptionist_state(
                    "starting",
                    "Starting the inbound GSM AI receptionist…"
                    if self.auto_answer_enabled
                    else "Warming the voice host so the next dial skips model loading…",
                )
                child_environment = self._child_environment(
                    auto_answer=self.auto_answer_enabled,
                    recording_consent=False,
                    call_channel="gsm",
                    # This host stays resident between calls, so an outbound
                    # dial can reuse its already-loaded models instead of
                    # paying the cold start again.
                    command_stdin=True,
                )
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "phone_agent_gateway.ai_bridge.phone_voice_agent",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=child_environment,
                )
                self._receptionist_process = process
                self._resident_host_environment_signature = (
                    self._voice_host_environment_signature(child_environment)
                )
                self._resident_host_ready = False
                self._resident_host_reported_config = {}
                if process.stdout is not None:
                    while line := await process.stdout.readline():
                        text = line.decode(errors="replace").strip()
                        self._write_raw_child_line(text)
                        # The warm host exists to hold the speech models, which
                        # is done well before the phone gateway is reachable.
                        # Waiting for the gateway to report "warm" meant that
                        # with the handset offline the host sat at "starting"
                        # forever even though its models were loaded and the next
                        # dial would already skip the ~20 s load.
                        if "gateway control ready" in text:
                            failures = 0
                            if self.auto_answer_enabled and self._resident_host_ready:
                                await self._set_receptionist_state(
                                    "listening", "Waiting for an incoming GSM call."
                                )
                            elif self._resident_host_ready:
                                await self._set_receptionist_state(
                                    "warm",
                                    "Voice host warm; the next dial skips model loading. "
                                    "Incoming calls are not answered.",
                                )
                        await self._handle_child_line(text)
                return_code = await process.wait()
                self._receptionist_process = None
                self._resident_host_environment_signature = None
                self._resident_host_ready = False
                self._resident_host_reported_config = {}
                # Only shutdown ends the supervisor. This previously also broke
                # when auto-answer was off, which -- once the warm host became
                # unconditional -- meant a host that exited for any reason was
                # never replaced, silently returning every later dial to the
                # ~20 s cold start this supervisor exists to avoid.
                if self._shutting_down:
                    break
                failures += 1
                delay = min(2**failures, 15)
                await self._set_receptionist_state(
                    "retrying",
                    f"Inbound receptionist exited with code {return_code}; retrying in {delay}s.",
                )
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Inbound receptionist supervisor failed: %s", exc)
            await self._set_receptionist_state("error", str(exc))
        finally:
            process = self._receptionist_process
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=8)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            self._receptionist_process = None

    async def _finish_call_bookkeeping(self) -> None:
        """Close out one call identically however its host was obtained."""

        duration = (
            max(0.0, time.monotonic() - self._call_started_at)
            if self._call_started_at is not None
            else 0.0
        )
        try:
            await asyncio.to_thread(
                self.audit_ledger.append,
                "call_ended",
                {
                    "destination": self.current_public_destination or "unknown",
                    "channel": self.config.call_channel,
                    "duration_seconds": round(duration, 3),
                },
            )
        except Exception:
            logger.exception("could not append call-ended audit event")
        self._active_process = None
        self._call_started_at = None
        self._warm_call_active = False
        await self.set_call_state("IDLE")
        if self._active_campaign_member_id:
            self._restore_campaign_settings()
        if self._restart_voice_host_after_call:
            self._restart_voice_host_after_call = False
            await self._stop_inbound_monitor()
        await self._start_inbound_monitor()

    def _note_warm_call_state(self, state: str) -> None:
        """Track a resident host's call through to its end."""

        if state in {"DIALING", "CONNECTING", "RINGING", "ACTIVE"}:
            self._warm_call_active = True
            self._warm_call_finished.clear()
        elif state in {"IDLE", "DISCONNECTED"} and self._warm_call_active:
            self._warm_call_active = False
            self._warm_call_finished.set()

    def _resident_host_stdin(self) -> asyncio.StreamWriter | None:
        """Return the warm host's command pipe when one is genuinely usable."""

        process = self._receptionist_process
        if process is None or process.returncode is not None:
            return None
        if not self._resident_host_matches_current_config():
            logger.error(
                "refusing resident voice host because its verified configuration is stale"
            )
            return None
        return process.stdin

    async def _dial_on_resident_host(
        self,
        writer: asyncio.StreamWriter,
        phone_number: str,
        *,
        recording_consent: bool = False,
    ) -> None:
        """Place a call on the already-loaded host and wait for it to finish.

        Spawning a host per call reloaded every local model first, which cost
        about six seconds before the phone even rang. The resident host has
        those models in memory already, so the dial reaches the carrier
        immediately.
        """

        self._warm_call_active = False
        self._warm_call_finished.clear()
        payload: dict[str, Any] = {"command": "dial", "number": phone_number}
        if recording_consent:
            payload["recording_consent"] = True
        command = json.dumps(payload)
        writer.write(command.encode() + b"\n")
        await writer.drain()
        try:
            async with asyncio.timeout(self.call_policy.config.max_call_duration_secs):
                await self._warm_call_finished.wait()
        except TimeoutError:
            await self.broadcast(
                {
                    "type": "call_notice",
                    "message": "The call reached the policy duration limit and was ended.",
                }
            )
            with contextlib.suppress(Exception):
                writer.write(json.dumps({"command": "hangup"}).encode() + b"\n")
                await writer.drain()

    async def _execute_dial(self, phone_number: str, *, recording_consent: bool) -> None:
        # Every call reuses the resident host when available and passes recording consent
        # dynamically to skip model loading and prevent process lock collisions.
        writer = self._resident_host_stdin()
        if writer is not None:
            self._child_reported_error = False
            try:
                await self._dial_on_resident_host(
                    writer, phone_number, recording_consent=recording_consent
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Resident-host dial failed: %s", exc)
                await self.broadcast({"type": "call_error", "message": str(exc)})
            finally:
                await self._finish_call_bookkeeping()
            return
        try:
            await self._stop_inbound_monitor()
            self._child_reported_error = False
            self._active_process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "phone_agent_gateway.ai_bridge.phone_voice_agent",
                "--dial",
                phone_number,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._child_environment(recording_consent=recording_consent),
            )
            async with asyncio.timeout(self.call_policy.config.max_call_duration_secs):
                if self._active_process.stdout is not None:
                    while line := await self._active_process.stdout.readline():
                        text = line.decode(errors="replace").strip()
                        # The filtered view drops anything unrecognised, which is how
                        # a traceback's frames — the part naming the actual fault —
                        # kept vanishing while only the word "Traceback" survived.
                        # The raw stream goes to the call log unfiltered.
                        self._write_raw_child_line(text)
                        await self._handle_child_line(text)
                return_code = await self._active_process.wait()
            if return_code and not self._child_reported_error:
                await self.broadcast(
                    {
                        "type": "call_error",
                        "message": f"Voice host exited unexpectedly (code {return_code}).",
                    }
                )
        except TimeoutError:
            await self.broadcast(
                {
                    "type": "call_notice",
                    "message": "The call reached the policy duration limit and was ended.",
                }
            )
            await self._terminate_owned_process()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Call process failed: %s", exc)
            await self.broadcast({"type": "call_error", "message": str(exc)})
        finally:
            await self._finish_call_bookkeeping()

    def _write_raw_child_line(self, line: str) -> None:
        _write_raw_child_line_to(RAW_CHILD_LOG, line)

    async def _handle_child_line(self, line: str) -> None:
        if line.startswith(EVENT_PREFIX):
            try:
                event = json.loads(line[len(EVENT_PREFIX) :])
            except json.JSONDecodeError:
                logger.warning("Ignored malformed PhoneAgent event")
                return
            if isinstance(event, dict):
                if event.get("type") == "voice_host_ready":
                    reported = event.get("config")
                    expected = self._expected_voice_host_config()
                    if not isinstance(reported, dict) or reported != expected:
                        self._resident_host_ready = False
                        self._resident_host_reported_config = (
                            dict(reported) if isinstance(reported, dict) else {}
                        )
                        logger.error(
                            "resident voice host rejected: effective configuration "
                            "does not match current Studio settings"
                        )
                        process = self._receptionist_process
                        if process is not None and process.returncode is None:
                            process.terminate()
                        await self._set_receptionist_state(
                            "error",
                            "Voice host configuration mismatch; restarting safely.",
                        )
                    else:
                        self._resident_host_reported_config = dict(reported)
                        self._resident_host_ready = True
                        if self.auto_answer_enabled:
                            await self._set_receptionist_state(
                                "starting",
                                "Voice models verified; connecting to the phone.",
                            )
                        else:
                            await self._set_receptionist_state(
                                "warm",
                                "Voice host configuration verified; ready for the next call.",
                            )
                if event.get("type") == "call_error":
                    self._child_reported_error = True
                if event.get("type") == "call_state":
                    self._note_warm_call_state(str(event.get("state", "")))
                if event.get("type") == "call_context" and event.get("caller_id"):
                    self.current_phone_number = str(event["caller_id"])
                if event.get("type") == "call_outcome":
                    event.setdefault("campaign_member", self._active_campaign_member_id)
                    self._spawn_background_task(self._sync_frappe_call_outcome(event))
                if event.get("type") in {
                    "tool_approval_required",
                    "tool_call",
                    "tools_reloaded",
                    "tools_reload_failed",
                }:
                    try:
                        audit_event = {
                            "tool_approval_required": "tool_approval_requested",
                            "tool_call": "tool_invoked",
                            "tools_reloaded": "live_tools_reloaded",
                            "tools_reload_failed": "live_tools_reload_failed",
                        }[str(event.get("type"))]
                        await asyncio.to_thread(
                            self.audit_ledger.append,
                            audit_event,
                            {
                                "tool_name": event.get("tool_name") or event.get("name"),
                                "request_id": event.get("request_id"),
                                "revision": event.get("revision"),
                                "active_tools": event.get("active_tools"),
                            },
                        )
                    except Exception:
                        logger.exception("could not append managed-tool audit event")
                if str(event.get("type") or "").startswith("openwa_"):
                    try:
                        await asyncio.to_thread(
                            self.audit_ledger.append,
                            "openwa_live_event",
                            {
                                "event_type": event.get("type"),
                                "message_id_hash": (
                                    hashlib.sha256(str(event.get("message_id")).encode()).hexdigest()[:16]
                                    if event.get("message_id")
                                    else None
                                ),
                                "status": event.get("status") or event.get("state"),
                            },
                        )
                    except Exception:
                        logger.exception("could not append OpenWA audit event")
                if str(event.get("type") or "").startswith("web_research_"):
                    try:
                        await asyncio.to_thread(
                            self.audit_ledger.append,
                            "web_research_live_event",
                            {
                                "event_type": event.get("type"),
                                "state": event.get("state"),
                                "confidence": event.get("confidence"),
                                "source_count": event.get("sources"),
                                "elapsed_ms": event.get("elapsed_ms"),
                                "revision": event.get("revision"),
                            },
                        )
                    except Exception:
                        logger.exception("could not append web research audit event")
                if str(event.get("type") or "").startswith("frappe_"):
                    try:
                        await asyncio.to_thread(
                            self.audit_ledger.append,
                            "frappe_live_event",
                            {
                                "event_type": event.get("type"),
                                "state": event.get("state"),
                                "tool_name": event.get("name"),
                                "verified": event.get("verified"),
                                "revision": event.get("revision"),
                            },
                        )
                    except Exception:
                        logger.exception("could not append Frappe audit event")
                await self.broadcast(event)
            return
        state_match = re.search(
            r"cellular state=(IDLE|RINGING|DIALING|CONNECTING|ACTIVE|HOLDING|DISCONNECTED)",
            line,
        )
        if state_match:
            state = state_match.group(1)
            await self.set_call_state("IDLE" if state == "DISCONNECTED" else state)
        elif any(
            marker in line
            for marker in (
                "Committed stable caller turn",
                "Replacing cross-language STT hypothesis",
                "Suppressed late STT revision",
                # A discarded caller turn must always be visible: silently
                # dropping one leaves the operator watching an empty screen.
                "Ignored caller backchannel",
                "Treated a short reply as an answer",
                "Discarded a hallucinated transcript",
                "Repaired caller turn",
                "Suppressed semantically incomplete caller fragment",
                "Bot-speaking state timed out",
            )
        ):
            # Keep the small set of authoritative STT decisions visible in the
            # Studio log. Other child output remains intentionally quiet.
            logger.info("Voice STT diagnostic: %s", line)
        elif any(
            marker in line
            for marker in (
                "Google TTS attempt failed",
                "Using Gemini TTS model fallback",
                "Gemini TTS model fallback failed",
                "Skipping quota-limited Gemini TTS primary",
                "Using Edge TTS fallback",
                "Edge TTS live attempt failed",
                "Edge TTS live retries exhausted",
                "Edge TTS stream failed after audio started",
                "Skipped phone audio end marker because TTS produced no PCM",
            )
        ):
            logger.warning("Voice TTS diagnostic: %s", line)
        elif "TTFB" in line or "processing time" in line.lower():
            # Pipecat's metrics observer reports per-service time-to-first-byte
            # and processing time. Without these the only visible latency is the
            # end-to-end turn number, which cannot say whether the LLM or the
            # TTS is responsible for a slow answer.
            logger.info("Voice latency metric: %s", line)
        elif any(
            marker in line
            for marker in (
                "Traceback (most recent call last)",
                "ConfigurationError",
                "VoiceHostBusyError",
                "[CRITICAL]",
                "[ERROR]",
            )
        ):
            # Child failures used to match no marker at all and were dropped, so
            # a crashed voice host produced a silent Studio and an empty log.
            logger.error("Voice host failure: %s", line)
            # A traceback's header matches, but its body — the frames and the
            # exception itself — matches nothing, so only the word "Traceback"
            # survived and the actual error was lost. Keep the frames that follow.
            self._forwarding_traceback = "Traceback (most recent call last)" in line
        elif getattr(self, "_forwarding_traceback", False) and (
            line.startswith((" ", "\t")) or _looks_like_exception(line)
        ):
            logger.error("Voice host failure: %s", line)
            self._forwarding_traceback = line.startswith((" ", "\t"))
        elif any(
            marker in line
            for marker in (
                "ChatGPT",
                "Realtime",
                "Gizmo",
                "WebRTC",
                "DataChannel",
                "audio stream connected",
                "Triggering ChatGPT",
                "Caller transcript",
                "Low-confidence caller transcription",
                "Caller transcription failed",
                "Caller transcription completed empty",
                "telephony media routes live",
                "caller audio quality summary",
                "call audio summary",
            )
        ):
            logger.info("Voice S2S diagnostic: %s", line)
        elif any(
            marker in line
            for marker in (
                "Uplink connection failed",
                "phone link disconnected",
                "phone media recovery failed",
                "recovered authenticated phone link in place",
                "opening greeting already attempted",
            )
        ):
            logger.warning("Voice gateway diagnostic: %s", line)

    async def handle_post_hangup(self, request: web.Request) -> web.Response:
        await self._terminate_owned_process()
        if self.call_state != "IDLE" and self._receptionist_process is not None:
            await self._stop_inbound_monitor()
        # Hang Up must also clear the dial task. Terminating the child alone
        # left the task pending, and every later dial answered 409 with no way
        # back except restarting the Studio.
        await self._cancel_dial_task()
        await self.set_call_state("IDLE")
        await self._start_inbound_monitor()
        return web.json_response({"status": "ok", "message": "Call ended."})

    async def _cancel_dial_task(self) -> None:
        task = self._dial_task
        self._dial_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _dial_in_progress(self) -> bool:
        """Report a real in-flight call, not a task that outlived its child.

        The task is only meaningful while it still owns a running child. If the
        child is gone the task is stale, and refusing new calls because of it
        strands the Studio permanently.
        """

        task = self._dial_task
        if task is None or task.done():
            return False
        process = self._active_process
        return process is not None and process.returncode is None

    async def _terminate_owned_process(self) -> None:
        process = self._active_process
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=8)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._ws_clients.add(ws)
        await ws.send_json(
            {
                "type": "status_sync",
                "call_state": self.call_state,
                "phone_number": self.current_public_destination,
                "config": self._public_config(),
            }
        )
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        json.loads(msg.data)
                    except json.JSONDecodeError:
                        await ws.send_json({"type": "error", "message": "Invalid JSON"})
                elif msg.type == WSMsgType.ERROR:
                    logger.warning("Studio WebSocket error: %s", ws.exception())
        finally:
            self._ws_clients.discard(ws)
        return ws

    async def set_call_state(self, state: str) -> None:
        self.call_state = state
        await self.broadcast({"type": "call_state", "state": state})

    async def broadcast(self, data: dict[str, Any]) -> None:
        self._control_event_sequence += 1
        control_event = _sanitize_openwa(data)
        if isinstance(control_event, dict):
            for key in ("caller_id", "phone_number", "destination"):
                value = control_event.get(key)
                if isinstance(value, str) and value and not value.startswith("sha256:"):
                    control_event[key] = public_destination(value, self.call_policy.salt)
            control_event = {
                "sequence": self._control_event_sequence,
                "observed_at": time.time(),
                **control_event,
            }
            self._control_events.append(control_event)
        if not self._ws_clients:
            return
        dead: list[web.WebSocketResponse] = []
        for ws in tuple(self._ws_clients):
            try:
                await ws.send_json(data)
            except (ConnectionError, RuntimeError):
                dead.append(ws)
        self._ws_clients.difference_update(dead)

    def _spawn_background_task(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _campaign_autopilot_loop(self) -> None:
        """Claim consent-eligible Frappe campaign members while Studio is idle."""

        while not self._shutting_down:
            delay = 15
            try:
                config = await asyncio.to_thread(self.frappe_config_store.load)
                delay = config.campaign_poll_seconds
                if (
                    not config.enabled
                    or not config.campaign_autopilot_enabled
                    or self.call_state != "IDLE"
                    or self._dial_in_progress()
                    or self._active_campaign_member_id
                ):
                    await asyncio.sleep(delay)
                    continue
                async with aiohttp.ClientSession() as session:
                    client = FrappeClient(config, session)
                    claimed = await client.call(
                        "next_campaign_contact",
                        {
                            "worker_id": self._campaign_worker_id,
                            "claim_seconds": config.campaign_claim_seconds,
                        },
                    )
                if not isinstance(claimed, dict) or not claimed.get("available"):
                    await asyncio.sleep(delay)
                    continue
                member_id = str(claimed.get("member_id") or "")
                phone = str(claimed.get("phone") or "")
                task_id = str(claimed.get("task_id") or self.task_id)
                channel = str(claimed.get("channel") or self.config.call_channel)
                self.task_engine.require_contract(task_id)
                if channel not in {"gsm", "whatsapp_phone", "whatsapp"}:
                    raise ValueError("campaign channel is invalid")
                self._campaign_original_task_id = self.task_id
                self._campaign_original_channel = self.config.call_channel
                self._active_campaign_member_id = member_id
                self.task_id = task_id
                self.config = replace(self.config, call_channel=channel)
                response = await self._begin_dial(
                    phone,
                    operator_approved=True,
                    recording_consent=False,
                )
                if response.status >= 300:
                    try:
                        body = json.loads(response.text)
                        failure = str(body.get("message") or "dial refused")
                    except Exception:
                        failure = "dial refused"
                    await self._complete_failed_campaign_claim(member_id, failure)
                    self._restore_campaign_settings()
                else:
                    await self.broadcast(
                        {
                            "type": "campaign_call_started",
                            "campaign_id": claimed.get("campaign_id"),
                            "member_id": member_id,
                            "attempt": claimed.get("attempt"),
                        }
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Campaign autopilot iteration failed: %s", exc, exc_info=True)
                await self.broadcast(
                    {"type": "campaign_autopilot_error", "message": str(exc)[:300]}
                )
                if self._active_campaign_member_id and not self._dial_in_progress():
                    await self._complete_failed_campaign_claim(
                        self._active_campaign_member_id, str(exc)
                    )
                    self._restore_campaign_settings()
            await asyncio.sleep(delay)

    async def _complete_failed_campaign_claim(self, member_id: str, message: str) -> None:
        try:
            config = await asyncio.to_thread(self.frappe_config_store.load)
            async with aiohttp.ClientSession() as session:
                await FrappeClient(config, session).call(
                    "complete_campaign_member",
                    {
                        "member_id": member_id,
                        "call_id": f"failed-{int(time.time())}",
                        "disposition": "failed",
                        "summary": str(message)[:500],
                    },
                )
        except Exception:
            logger.exception("Could not release failed Frappe campaign claim")

    def _restore_campaign_settings(self) -> None:
        if self._campaign_original_task_id:
            self.task_id = self._campaign_original_task_id
        if self._campaign_original_channel:
            self.config = replace(
                self.config,
                call_channel=self._campaign_original_channel,
            )
        self._active_campaign_member_id = ""
        self._campaign_original_task_id = ""
        self._campaign_original_channel = ""

    async def _sync_frappe_call_outcome(self, event: dict[str, Any]) -> None:
        try:
            config = await asyncio.to_thread(self.frappe_config_store.load)
            if not config.enabled:
                return
            outcome = str(event.get("outcome") or "abandoned")
            disposition_map = {
                "completed": "converted",
                "qualified": "qualified",
                "refused": "not_interested",
                "callback": "callback",
                "failed": "failed",
                "abandoned": "connected",
            }
            caller = str(event.get("caller_id") or self.current_phone_number)
            call_id = str(event.get("call_id") or f"studio-{int(time.time())}")
            duration = (
                max(0.0, time.monotonic() - self._call_started_at)
                if self._call_started_at is not None
                else 0.0
            )
            async with aiohttp.ClientSession() as session:
                await FrappeClient(config, session).call(
                    "record_call_outcome",
                    {
                        "phone": caller,
                        "call_id": call_id,
                        "call_direction": str(event.get("direction") or "outbound"),
                        "task_id": str(event.get("task_id") or self.task_id),
                        "disposition": disposition_map.get(outcome, "connected"),
                        "summary": json.dumps(event, ensure_ascii=False, default=str)[:2_000],
                        "channel": str(event.get("channel") or self.config.call_channel),
                        "duration_seconds": duration,
                        "structured_outcome": event,
                        "campaign_member": str(event.get("campaign_member") or ""),
                    },
                )
            await self.broadcast(
                {
                    "type": "frappe_call_synced",
                    "call_id": call_id,
                    "campaign_member": event.get("campaign_member") or None,
                }
            )
        except Exception as exc:
            logger.error("Frappe call outcome sync failed: %s", exc, exc_info=True)
            await self.broadcast(
                {"type": "frappe_call_sync_failed", "message": str(exc)[:300]}
            )

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        logger.info("PhoneAgent Studio is available at http://%s:%d", self.host, self.port)

    async def _start_remote_link(self) -> None:
        """Accept a handset that dials in instead of hanging off a cable."""

        if not self._remote_link_settings.enabled:
            return
        try:
            relay = RemoteLinkRelay(
                load_remote_link_key(),
                listen_host=self._remote_link_settings.listen_host,
                listen_port=self._remote_link_settings.listen_port,
            )
            await relay.start()
        except Exception as exc:
            # A relay that cannot bind must not stop a cabled phone from working.
            logger.exception("remote link relay could not start")
            self._remote_link_error = str(exc)
            return
        self._remote_link_error = ""
        self._remote_link = relay
        logger.info(
            "remote link relay accepting a handset on %s:%d",
            self._remote_link_settings.listen_host,
            self._remote_link_settings.listen_port,
        )

    async def _restart_remote_link_for_new_key(self) -> None:
        """Reload the relay so it verifies against the key that was just written."""

        if self._remote_link is None:
            return
        await self._remote_link.close()
        self._remote_link = None
        await self._start_remote_link()
        logger.info("remote link relay reloaded with the rotated key")

    def remote_link_status(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "enabled": self._remote_link_settings.enabled,
            "running": self._remote_link is not None,
            "listen_port": self._remote_link_settings.listen_port,
            # Shown in Studio so the address can be read off the screen and
            # typed into the handset, instead of hunted for in a terminal.
            "addresses": local_addresses(),
            "error": self._remote_link_error,
            **pairing_status(
                (local_addresses() or [""])[0],
                self._remote_link_settings.listen_port,
            ),
        }
        if self._remote_link is not None:
            base.update(self._remote_link.stats.snapshot())
        return base

    async def set_remote_link(self, *, enabled: bool, port: int | None = None) -> dict[str, Any]:
        """Turn the tunnel on or off from Studio, with no restart."""

        if port is not None:
            if not 1 <= port <= 65535:
                raise ValueError("port must be between 1 and 65535")
            self._remote_link_settings.listen_port = port
        self._remote_link_settings.enabled = enabled
        await asyncio.to_thread(self._remote_link_settings.save)

        if self._remote_link is not None:
            await self._remote_link.close()
            self._remote_link = None
        if enabled:
            await self._start_remote_link()
            if self._remote_link is None:
                raise RuntimeError(
                    self._remote_link_error
                    or f"could not listen on port {self._remote_link_settings.listen_port}"
                )
        # A resident host spawned before the tunnel came up still believes it
        # must create adb forwards. Restarting it is what makes the transport
        # switch take effect without an operator restarting anything.
        await self._stop_inbound_monitor()
        await self._start_inbound_monitor()
        await self.broadcast({"type": "remote_link_updated", **self.remote_link_status()})
        return self.remote_link_status()

    async def _on_startup(self, app: web.Application) -> None:
        await self._start_remote_link()
        await self._start_inbound_monitor()
        self._spawn_background_task(self._campaign_autopilot_loop())
        self._spawn_background_task(self._prewarm_gpu_models_task())

    async def _on_shutdown(self, app: web.Application) -> None:
        self._shutting_down = True
        if self._remote_link is not None:
            await self._remote_link.close()
            self._remote_link = None
        await self._stop_inbound_monitor()
        await self._terminate_owned_process()
        tasks = [task for task in self._background_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._shutting_down = True
        await self._stop_inbound_monitor()
        await self._terminate_owned_process()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


async def run_server(host: str = DEFAULT_WEB_HOST, port: int = DEFAULT_WEB_PORT) -> None:
    server = PhoneAgentWebServer(host=host, port=port)
    await server.start()
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()


def main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="PhoneAgent Studio")
    parser.add_argument(
        "--host",
        default=os.getenv("PHONE_AGENT_WEB_HOST", DEFAULT_WEB_HOST),
        help="Host address",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PHONE_AGENT_WEB_PORT", str(DEFAULT_WEB_PORT))),
        help="Port",
    )
    args = parser.parse_args()
    server = PhoneAgentWebServer(host=args.host, port=args.port)
    web.run_app(server.app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
