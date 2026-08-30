from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from phone_agent_gateway.ai_bridge.frappe_integration import (
    FrappeConfig,
    FrappeConfigStore,
    FrappeToolRuntime,
)
from phone_agent_gateway.ai_bridge.tasks.tool_catalog import execute_tool
from phone_agent_gateway.ai_bridge.tool_control import MASKED_SECRET


def _config(base_url: str, *enabled: str) -> FrappeConfig:
    tools = [
        policy.model_copy(
            update={"enabled": policy.name in enabled, "task_ids": ["iptv"]}
        )
        for policy in FrappeConfig().tools
    ]
    return FrappeConfig(
        enabled=True,
        base_url=base_url,
        api_key="integration-key",
        api_secret="integration-secret",
        tools=tools,
    )


def test_config_masks_both_credentials_and_preserves_them(tmp_path: Path) -> None:
    store = FrappeConfigStore(tmp_path / "frappe.json")
    saved = store.save(
        FrappeConfig(
            api_key="private-key",
            api_secret="private-secret",
        ).model_dump(mode="json")
    )

    assert saved.revision == 1
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    public = store.public_state()
    assert public["api_key"] == MASKED_SECRET
    assert public["api_secret"] == MASKED_SECRET
    assert "private" not in json.dumps(public)

    public["enabled"] = True
    updated = store.save(public)
    assert updated.api_key == "private-key"
    assert updated.api_secret == "private-secret"


def test_remote_plain_http_and_incomplete_activation_fail_closed() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        FrappeConfig(base_url="http://erp.example.com")
    with pytest.raises(ValueError, match="requires an API key"):
        FrappeConfig(enabled=True)


@pytest.mark.asyncio
async def test_live_business_tools_are_bound_to_authenticated_current_caller() -> None:
    requests: list[dict] = []

    async def method(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "token integration-key:integration-secret"
        name = request.match_info["method"]
        payload = await request.json()
        requests.append({"method": name, "payload": payload})
        if name == "health":
            return web.json_response(
                {
                    "message": {
                        "status": "ok",
                        "site": "phoneagent.localhost",
                        "required_ready": True,
                    }
                }
            )
        return web.json_response(
            {"message": {"verified": True, "phone_received": payload["phone"]}}
        )

    app = web.Application()
    app.router.add_post("/api/method/phoneagent_frappe.api.{method}", method)
    async with TestServer(app) as upstream:
        runtime = FrappeToolRuntime(
            _config(
                str(upstream.make_url("/"))[:-1],
                "business_get_customer_context",
                "business_upsert_current_lead",
            ),
            caller_id="+212600123456",
            task_id="iptv",
            call_id="call-1",
            call_direction="inbound",
        )
        try:
            catalog = await runtime.start()
            definition = catalog["business_get_customer_context"].definition
            assert "phone" not in definition["parameters"]["properties"]
            result = json.loads(
                await execute_tool(catalog, "business_get_customer_context", "{}")
            )
        finally:
            await runtime.close()

    assert result == {"verified": True, "phone_received": "<redacted-phone>"}
    assert requests[-1]["payload"] == {
        "phone": "212600123456",
        "call_id": "call-1",
        "task_id": "iptv",
        "call_direction": "inbound",
        "max_items": 10,
    }


def test_unified_compose_is_local_persistent_and_resource_bounded() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = (root / "integrations/business_suite/compose.yaml").read_text()

    assert "phoneagent-frappe-suite:1.0.0" in compose
    assert '127.0.0.1:${FRAPPE_PORT:-8080}:8080' in compose
    assert "frappe-db: {name: phoneagent-frappe-db}" in compose
    assert "no-new-privileges:true" in compose
    assert "pids_limit:" in compose and "mem_limit:" in compose
    assert "frappe_db_root_password:" in compose
    assert "phoneagent-openwa-data" in compose
    assert "@sha256:" in compose


def test_frappe_app_has_campaign_consent_and_draft_commerce_boundaries() -> None:
    root = Path(__file__).resolve().parents[2]
    app = root / "integrations/business_suite/phoneagent_frappe/phoneagent_frappe"
    api = (app / "api.py").read_text()
    campaign = (
        app
        / "phoneagent_automation/doctype/phoneagent_campaign/phoneagent_campaign.json"
    ).read_text()

    assert "def next_campaign_contact" in api
    assert "def mark_do_not_call" in api
    assert "def create_quotation_draft" in api
    assert "def create_sales_order_draft" in api
    assert "draft_not_submitted" in api
    assert "for update skip locked" in api
    assert '"require_explicit_consent"' in campaign
    assert '"max_daily_calls"' in campaign


def test_macos_installer_handles_both_supported_architectures() -> None:
    root = Path(__file__).resolve().parents[2]
    installer = (root / "tools" / "install_macos.sh").read_text()

    assert 'arm64) REQUIRED_BINARY_ARCH="arm64"' in installer
    assert 'x86_64) REQUIRED_BINARY_ARCH="x86_64"' in installer
    assert "cargo build --locked --release" in installer
    assert 'cp "${WHATSAPP_BINARY_SOURCE}"' in installer
