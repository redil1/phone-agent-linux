"""Unit tests for ChatGPT Realtime Auth & Token Management."""

from __future__ import annotations

import base64
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import (
    ChatGPTAuthManager,
    decode_jwt_payload,
)


def _make_dummy_jwt(
    client_id: str = "test_client",
    email: str = "test@example.com",
    plan: str = "chatgpt_plan_type",
    exp: float | None = None,
    scopes: list[str] | None = None,
) -> str:
    payload: dict[str, object] = {
        "client_id": client_id,
        "exp": exp if exp is not None else time.time() + 3600,
        "https://api.openai.com/profile": {"email": email},
        "https://api.openai.com/auth": {"chatgpt_plan_type": plan},
    }
    if scopes is not None:
        payload["scp"] = scopes
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.dummy_signature"


def test_decode_jwt_payload() -> None:
    claims = decode_jwt_payload(_make_dummy_jwt(client_id="my_app"))
    assert claims["client_id"] == "my_app"
    assert claims["https://api.openai.com/profile"]["email"] == "test@example.com"


def test_decode_jwt_invalid() -> None:
    assert decode_jwt_payload("not-a-jwt") == {}


def test_auth_manager_from_cache(tmp_path) -> None:
    cache_file = tmp_path / "session_cache.json"
    codex_file = tmp_path / "codex_auth.json"
    token = _make_dummy_jwt()
    cache_file.write_text(
        json.dumps(
            {
                "accessToken": token,
                "refresh_token": "rt_12345",
                "user": {"email": "test@example.com"},
            }
        ),
        encoding="utf-8",
    )
    with patch.dict("os.environ", {"CHATGPT_ACCESS_TOKEN": ""}, clear=False):
        manager = ChatGPTAuthManager(cache_file=cache_file, codex_auth_path=codex_file)
    assert manager.get_token() == token
    assert manager.is_token_expired() is False
    assert manager.user_info["email"] == "test@example.com"


def test_auth_manager_token_expired(tmp_path) -> None:
    cache_file = tmp_path / "session_cache.json"
    codex_file = tmp_path / "codex_auth.json"
    cache_file.write_text(
        json.dumps(
            {
                "accessToken": _make_dummy_jwt(exp=time.time() - 10),
                "refresh_token": "rt_expired",
            }
        ),
        encoding="utf-8",
    )
    with patch.dict("os.environ", {"CHATGPT_ACCESS_TOKEN": ""}, clear=False):
        manager = ChatGPTAuthManager(cache_file=cache_file, codex_auth_path=codex_file)
    assert manager.is_token_expired() is True


def test_auth_manager_refresh_success(tmp_path) -> None:
    cache_file = tmp_path / "session_cache.json"
    codex_file = tmp_path / "codex_auth.json"
    token_old = _make_dummy_jwt(exp=time.time() - 10)
    token_new = _make_dummy_jwt(exp=time.time() + 3600)
    cache_file.write_text(
        json.dumps({"accessToken": token_old, "refresh_token": "rt_old"}),
        encoding="utf-8",
    )
    with patch.dict("os.environ", {"CHATGPT_ACCESS_TOKEN": ""}, clear=False):
        manager = ChatGPTAuthManager(cache_file=cache_file, codex_auth_path=codex_file)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"access_token": token_new, "refresh_token": "rt_new"}
    mock_session_instance = MagicMock()
    mock_session_instance.post.return_value = mock_resp
    mock_session_instance.__enter__.return_value = mock_session_instance
    mock_session_instance.__exit__.return_value = False

    with patch(
        "phone_agent_gateway.ai_bridge.chatgpt_realtime_auth.Session",
        return_value=mock_session_instance,
    ):
        assert manager.get_token() == token_new
    assert manager._refresh_token == "rt_new"


def test_auth_manager_from_codex_auth(tmp_path) -> None:
    cache_file = tmp_path / "session_cache.json"
    codex_file = tmp_path / "codex_auth.json"
    token = _make_dummy_jwt()
    codex_file.write_text(
        json.dumps({"tokens": {"access_token": token, "refresh_token": "rt_codex_999"}}),
        encoding="utf-8",
    )
    with patch.dict("os.environ", {"CHATGPT_ACCESS_TOKEN": ""}, clear=False):
        manager = ChatGPTAuthManager(cache_file=cache_file, codex_auth_path=codex_file)
    assert manager.get_token() == token
    assert manager._refresh_token == "rt_codex_999"


def test_auth_manager_no_token(tmp_path) -> None:
    with (
        patch.dict("os.environ", {"CHATGPT_ACCESS_TOKEN": ""}, clear=False),
        pytest.raises(RuntimeError, match="No ChatGPT access token available"),
    ):
        ChatGPTAuthManager(
            cache_file=tmp_path / "nonexistent.json",
            codex_auth_path=tmp_path / "nonexistent_codex.json",
        ).get_token()
