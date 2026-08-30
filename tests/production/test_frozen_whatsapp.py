from __future__ import annotations

from phone_agent_gateway.tools.verify_frozen_whatsapp import verify


def test_qualified_whatsapp_files_are_byte_for_byte_frozen() -> None:
    assert verify() == []
