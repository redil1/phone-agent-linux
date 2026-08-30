import asyncio
import logging
from unittest.mock import MagicMock

from phone_agent_gateway.ai_bridge.chatgpt_realtime_pipeline import ChatGPTRealtimePipeline
from phone_agent_gateway.ai_bridge.pipecat_transport import PhoneAgentTransport
from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig, RuntimeConfig
from phone_agent_gateway.ai_bridge.session import CallSessionState, SessionPhase

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

async def run_e2e_test():
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
        system_prompt=(
            "You are Aziz, Senior Sales Consultant at IPTV Shopping. Preserve that identity "
            "exactly and execute the active task without repeating the opening."
        ),
        link_authentication_key=b"0" * 32,
        providers=providers,
    )

    session = CallSessionState()
    session.set_phase(SessionPhase.CONNECTING)
    session.set_phase(SessionPhase.ACTIVE)
    transport = PhoneAgentTransport(session=session)

    audio_frames_written = []
    async def capture_audio(payload, generation_id, sequence):
        audio_frames_written.append(payload)

    async def acknowledge_audio_end(generation_id, sequence):
        session.mark_rendered(generation_id, sequence)

    transport.set_tx_handler(capture_audio)
    transport.set_audio_end_handler(acknowledge_audio_end)
    events = []

    pipeline = ChatGPTRealtimePipeline(
        transport=transport,
        config=config,
        caller_id="+33612345678",
        event_sink=events.append,
    )

    print("\n--- 1. STARTING PIPELINE ---")
    await pipeline.start()
    print("Pipeline started successfully!")

    print("\n--- 2. CALLING GREET ---")
    await pipeline.greet()
    print("Greet called!")

    try:
        print("\n--- 3. LISTENING UNTIL PHONE PLAYBACK COMPLETES ---")
        deadline = asyncio.get_running_loop().time() + 18.0
        while asyncio.get_running_loop().time() < deadline:
            playback = [event for event in events if event.get("type") == "playback_status"]
            if playback and playback[-1]["status"] in {
                "completed",
                "failed",
                "not_delivered",
            }:
                break
            await asyncio.sleep(0.1)

        print("\n--- 4. RESULTS ---")
        print(f"Total audio frames delivered to phone earpiece: {len(audio_frames_written)}")
        total_bytes = sum(len(f) for f in audio_frames_written)
        print(
            f"Total audio bytes: {total_bytes} "
            f"(approx {total_bytes / 32000:.2f} seconds of 16kHz audio)"
        )
        print(f"Spoken text transcript:\n{pipeline.policy.last_spoken_turn()}")

        assert audio_frames_written, "Realtime returned no phone-ready audio"
        transcript = pipeline.policy.last_spoken_turn()
        assert "Aziz" in transcript and "IPTV Shopping" in transcript
        duration = total_bytes / 32000
        assert duration < 12.0, "Idle WebRTC media leaked into phone playout"
        playback = [event for event in events if event.get("type") == "playback_status"]
        assert playback[-1]["status"] == "completed"
    finally:
        await pipeline.close()

if __name__ == "__main__":
    asyncio.run(run_e2e_test())
