import asyncio

from phone_agent_gateway.ai_bridge.runtime_config import RuntimeConfig
from phone_agent_gateway.ai_bridge.session import CallSessionState
from phone_agent_gateway.mac_client.framed_link import LinkPorts
from phone_agent_gateway.mac_client.protocol_client import AuthenticatedPhoneAgentClient


async def check():
    config = RuntimeConfig.from_env(require_provider_credentials=False)
    ports = LinkPorts(
        legacy_http=config.control_port,
        downlink=config.rx_port,
        uplink=config.tx_port,
        control=config.protocol_control_port,
    )
    session = CallSessionState()
    client = AuthenticatedPhoneAgentClient(
        session=session,
        authentication_key=config.link_authentication_key,
        host=config.control_host,
        ports=ports,
    )
    try:
        print("Connecting control channel...")
        client.connect_control()
        print("Connected!")
        status = client.get_status()
        print("Call status:", status)
        audio = client.get_audio_status()
        print("Audio status report:", audio)
        health = client.get_health()
        print("Gateway health:", health)
        client.close()
    except Exception as e:
        print("Hardware probe error:", e)

if __name__ == "__main__":
    asyncio.run(check())
