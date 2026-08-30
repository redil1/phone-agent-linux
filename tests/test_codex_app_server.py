"""Content conversion tests for the Codex app-server Pipecat service."""

from phone_agent_gateway.ai_bridge.codex_app_server import CodexAppServerLLMService
from pipecat.processors.aggregators.llm_context import LLMContext


def test_latest_user_text_supports_string_content() -> None:
    context = LLMContext(
        [
            {"role": "system", "content": "system"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "What time is it?"},
        ]
    )
    assert CodexAppServerLLMService._latest_user_text(context) == "What time is it?"


def test_latest_user_text_supports_universal_content_parts() -> None:
    context = LLMContext(
        [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "First"},
                    {"type": "text", "text": "second"},
                ],
            }
        ]
    )
    assert CodexAppServerLLMService._latest_user_text(context) == "First second"

