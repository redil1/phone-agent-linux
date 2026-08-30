"""Contained live GA regression for caller barge-in response serialization.

Usage: python s2s_research/test_e2e_realtime_barge_in.py /path/to/pcm_s16le_16k_mono.raw
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

from phone_agent_gateway.ai_bridge.chatgpt_realtime_pipeline import (
    PHONE_CHUNK_BYTES,
    ChatGPTRealtimePipeline,
)
from phone_agent_gateway.ai_bridge.pipecat_transport import PhoneAgentTransport
from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig, RuntimeConfig
from phone_agent_gateway.ai_bridge.session import CallSessionState, SessionPhase

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


async def run_barge_in_test(caller_pcm_path: Path) -> None:
    caller_pcm = await asyncio.to_thread(caller_pcm_path.read_bytes)
    if not caller_pcm:
        raise ValueError("caller PCM is empty")

    providers = ProviderConfig(
        pipeline_mode="s2s_chatgpt_realtime",
        chatgpt_realtime_voice="alloy",
        chatgpt_realtime_model="auto",
        stt_language="en-US",
    )
    config = RuntimeConfig(
        device_id="test_device",
        control_host="127.0.0.1",
        control_port=8765,
        protocol_control_port=8768,
        rx_port=8766,
        tx_port=8767,
        sample_rate=16000,
        frame_ms=20,
        input_queue_frames=25,
        auto_answer=False,
        record_calls=False,
        memory_enabled=False,
        task_id="iptv_shopping_sales",
        event_stream_enabled=True,
        voice_lock_path=MagicMock(),
        system_prompt="You are Aziz from IPTV Shopping. Stay in persona and answer concisely.",
        link_authentication_key=b"0" * 32,
        providers=providers,
    )
    session = CallSessionState()
    session.set_phase(SessionPhase.CONNECTING)
    session.set_phase(SessionPhase.ACTIVE)
    transport = PhoneAgentTransport(session=session)
    phone_frames: list[bytes] = []
    greeting_audio_started = asyncio.Event()
    conversation_completed = asyncio.Event()
    events: list[dict] = []

    async def capture_audio(payload: bytes, _generation_id: int, _sequence: int) -> None:
        phone_frames.append(payload)
        if len(phone_frames) >= 20:
            greeting_audio_started.set()

    async def acknowledge_audio_end(generation_id: int, sequence: int) -> None:
        session.mark_rendered(generation_id, sequence)

    async def acknowledge_flush(_generation_id: int) -> dict[str, str]:
        return {"status": "ok"}

    def capture_event(event: dict) -> None:
        events.append(event)
        user_turns = [
            item
            for item in events
            if item.get("type") == "transcript" and item.get("role") == "user"
        ]
        assistant_turns = [
            item
            for item in events
            if item.get("type") == "transcript" and item.get("role") == "assistant"
        ]
        completed = [
            item
            for item in events
            if item.get("type") == "playback_status" and item.get("status") == "completed"
        ]
        if user_turns and len(assistant_turns) >= 2 and completed:
            conversation_completed.set()

    transport.set_tx_handler(capture_audio)
    transport.set_audio_end_handler(acknowledge_audio_end)
    transport.set_flush_handler(acknowledge_flush)
    pipeline = ChatGPTRealtimePipeline(
        transport=transport,
        config=config,
        caller_id="contained-live-test",
        event_sink=capture_event,
    )

    await pipeline.start()
    control_events: list[dict] = []
    original_send = pipeline.send_event

    def capture_control(event: dict) -> None:
        control_events.append(event)
        original_send(event)

    pipeline.send_event = capture_control  # type: ignore[method-assign]
    try:
        await pipeline.greet()
        try:
            await asyncio.wait_for(greeting_audio_started.wait(), timeout=8.0)
        except TimeoutError:
            pass
        assert phone_frames, "greeting audio never started"

        media_track = pipeline.media_track
        assert media_track is not None
        for offset in range(0, len(caller_pcm), PHONE_CHUNK_BYTES):
            frame = caller_pcm[offset : offset + PHONE_CHUNK_BYTES]
            if len(frame) < PHONE_CHUNK_BYTES:
                frame += b"\x00" * (PHONE_CHUNK_BYTES - len(frame))
            media_track.push_pcm_frame(frame)
            await asyncio.sleep(0.02)

        try:
            await asyncio.wait_for(conversation_completed.wait(), timeout=25.0)
        except TimeoutError:
            pass

        control_types = [event.get("type") for event in control_events]
        errors = [event for event in events if event.get("type") == "call_error"]
        user_text = " ".join(
            str(event.get("text", ""))
            for event in events
            if event.get("type") == "transcript" and event.get("role") == "user"
        )
        print("Control sequence:", control_types)
        print("Caller transcript:", user_text)
        print("Errors:", errors)

        assert "response.cancel" in control_types
        assert "output_audio_buffer.clear" in control_types
        assert control_types.count("response.create") == 2
        assert not errors
        assert user_text
    finally:
        await pipeline.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("provide one raw pcm_s16le_16k_mono file")
    asyncio.run(run_barge_in_test(Path(sys.argv[1])))
