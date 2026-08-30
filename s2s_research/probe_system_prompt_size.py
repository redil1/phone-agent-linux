import asyncio
import json
from aiortc import RTCPeerConnection, AudioStreamTrack
from curl_cffi.requests import AsyncSession
from phone_agent_gateway.ai_bridge.chatgpt_realtime_auth import ChatGPTAuthManager
from phone_agent_gateway.ai_bridge.personality.persona_compiler import PersonaCompiler
from phone_agent_gateway.ai_bridge.tasks.task_engine import TaskEngine

class DummyTrack(AudioStreamTrack):
    kind = "audio"

async def test():
    auth = ChatGPTAuthManager()
    token = auth.get_token()

    compiler = PersonaCompiler()
    task_engine = TaskEngine()
    contract = task_engine.require_contract("iptv_subscription_sales")
    system_prompt = compiler.compile(task_contract=contract, language="fr-FR")

    print(f"System prompt length: {len(system_prompt)} chars")

    pc = RTCPeerConnection()
    pc.addTrack(DummyTrack())
    dc = pc.createDataChannel("oai-events")

    @dc.on("open")
    def on_open():
        print("DataChannel open! Sending full compiled system prompt...")
        session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": system_prompt,
                "audio": {
                    "output": {
                        "voice": "coral"
                    }
                }
            }
        }
        dc.send(json.dumps(session_update))

    @dc.on("message")
    def on_msg(msg):
        ev = json.loads(msg)
        t = ev.get("type", "")
        print("DC Event:", t)
        if t == "error":
            print("ERROR DETAILS:", json.dumps(ev, indent=2))
        elif t == "session.updated":
            print("SESSION.UPDATED CONFIRMED!")

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    url = "https://api.openai.com/v1/realtime/calls?model=gpt-realtime-1.5"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/sdp",
    }

    async with AsyncSession(impersonate="safari17_0") as session:
        res = await session.post(url, headers=headers, data=pc.localDescription.sdp, timeout=15)
        if res.status_code == 201:
            from aiortc import RTCSessionDescription
            await pc.setRemoteDescription(RTCSessionDescription(sdp=res.text, type="answer"))

    await asyncio.sleep(4.0)
    await pc.close()

if __name__ == "__main__":
    asyncio.run(test())
