"""Safety and context conversion tests for the Gemini CLI adapter."""

from pipecat.processors.aggregators.llm_context import LLMContext

from phone_agent_gateway.ai_bridge.gemini_cli import GeminiCliLLMService, _safe_error_summary


def test_render_context_preserves_conversation_and_instruction() -> None:
    service = GeminiCliLLMService(
        model="gemini-test",
        system_instruction="Be concise.",
        binary="/usr/bin/true",
    )
    context = LLMContext(
        [
            {"role": "assistant", "content": "Hello."},
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Can you help?"}],
            },
        ]
    )

    prompt = service._render_context(context)

    assert "SYSTEM:\nBe concise." in prompt
    assert "ASSISTANT:\nHello." in prompt
    assert "USER:\nCan you help?" in prompt
    assert "Do not use tools" in prompt


def test_error_summary_redacts_email_and_token() -> None:
    summary = _safe_error_summary(
        "account: person@example.com\nauthorization=very-secret-value\nfailed"
    )

    assert "person@example.com" not in summary
    assert "very-secret-value" not in summary
    assert "<redacted-email>" in summary
    assert "<redacted>" in summary


def test_error_summary_keeps_cause_and_drops_stack_frames() -> None:
    summary = _safe_error_summary(
        "An unexpected critical error occurred:\n"
        "Error: This account requires setting GOOGLE_CLOUD_PROJECT.\n"
        "    at setupUser (file:///private/module.js:1:2)\n"
    )

    assert "requires setting GOOGLE_CLOUD_PROJECT" in summary
    assert "at setupUser" not in summary
