# Source Map

Use this map to open the smallest authoritative set of files. Search with `rg` before broad reading.

## Entrypoints and orchestration

| File | Responsibility |
| --- | --- |
| `ai_bridge/web_server.py` | Persistent Studio, REST/WebSocket, child lifecycle, campaigns, control API |
| `ai_bridge/phone_voice_agent.py` | Per-call host, status loop, prewarm/preconnect, media attach, pipeline lifecycle |
| `ai_bridge/runtime_config.py` | Environment/runtime dataclasses and compatibility validation |
| `ai_bridge/production_pipeline.py` | Cascade provider construction, prewarming and Pipecat call pipeline |
| `ai_bridge/local_control.py` | Private local control token and loopback request client |
| `ai_bridge/mcp_server.py` | External stdio MCP resources/tools |
| `ai_bridge/control_plane.py` | AgentPackage, RuntimeControl, deployment records/store/hashes |

## Realtime and conversation

| File | Responsibility |
| --- | --- |
| `ai_bridge/openai_realtime_websocket_pipeline.py` | Qualified direct Realtime PCM transport |
| `ai_bridge/chatgpt_realtime_pipeline.py` | Realtime WebRTC compatibility transport |
| `ai_bridge/chatgpt_realtime_auth.py` | Local Codex/ChatGPT OAuth token loading/refresh |
| `ai_bridge/agent_policy.py` | Prompt composition, transcript observation, language, continuity, evaluation |
| `ai_bridge/call_context.py` | Inbound intent/outbound prospecting phase policy |
| `ai_bridge/tool_argument_grounding.py` | Caller-literal durable-write grounding |
| `ai_bridge/conversation_repair.py` | Turn-quality classification and repair language |
| `ai_bridge/conversational_reflex.py` | Bounded immediate conversational reactions |
| `ai_bridge/speculative_turn.py` | Low-latency provisional prefetch coordination |
| `ai_bridge/turn_continuity.py` | Semantic incompleteness/revision helpers |
| `ai_bridge/duplex_echo_gate.py` | Echo/low-noise suppression without inventing turns |
| `ai_bridge/human_speech.py` | Speech phrasing and language helpers |

## Media and device link

| File | Responsibility |
| --- | --- |
| `ai_bridge/media_protocol.py` | Authenticated binary media/control frame schema |
| `ai_bridge/session.py` | Call phase, generation, metrics and conversation coordinator |
| `ai_bridge/pipecat_transport.py` | Phone input/output transport, credit/playback/generation handling |
| `ai_bridge/voice_host_lock.py` | Single voice-host ownership |
| `mac_client/protocol_client.py` | Authenticated Android control client |
| `mac_client/framed_link.py` | Framed media link, queues, reconnect and telemetry |
| `mac_client/gateway_client.py` | Gateway status/control API types |
| `mac_client/audio_bridge.py` | Legacy/raw bridge utilities |

## Android gateway

| File | Responsibility |
| --- | --- |
| `android_service_apk/.../GatewayService.java` | Android privileged service and servers |
| `.../PhoneAgentInCallService.java` | Telecom callbacks and physical mic control |
| `.../CallManager.java` | Dial/answer/hangup/status |
| `.../DigitalAudioBridge.java` | Caller capture, AI injection, playout metrics |
| `.../ProtocolControlServer.java` | Authenticated idempotent control/replay journal |
| `.../ProtocolCodec.java` | Wire codec |
| `.../LinkKeyStore.java` | Link authentication key |
| `.../LinkSessionRegistry.java` | Epoch/session ownership |
| `.../VoipAudioRoute.java` | Frozen Android WhatsApp/VoIP media route |
| `android_service_apk/build_and_install.sh` | APK build/install workflow |
| `android_service_apk/install_privileged.sh` | Privileged provisioning |

## Identity, tasks and memory

| File | Responsibility |
| --- | --- |
| `ai_bridge/identity/models.py` | Strict identity, memory, evaluation and revision schemas |
| `ai_bridge/identity/kernel.py` | Identity composition, skills, memory context and lifecycle |
| `ai_bridge/identity/store.py` | Private files, revisions/history/trust/mutable blocks/audit |
| `ai_bridge/identity/evaluation.py` | Replayable identity contract evaluator |
| `ai_bridge/identity/skills.py` | Skill draft/definition/discovery/digest trust |
| `ai_bridge/identity/memory.py` | Local episodes and optional Graphiti mirror |
| `ai_bridge/tasks/task_engine.py` | Strict task contract validation/persistence |
| `ai_bridge/tasks/call_state.py` | Runtime slots, stages and outcomes |
| `ai_bridge/tasks/tool_catalog.py` | Built-in/task-filtered Realtime catalog and execution |
| `ai_bridge/tasks/tool_registry.py` | User Python tool registry/runtime |
| `ai_bridge/tasks/evalset.py` | Scenario evaluation |
| `ai_bridge/tasks/product_pipeline.py` | Product-site task authoring orchestration |
| `ai_bridge/tasks/product_import.py` | Claim verification and contract generation |
| `ai_bridge/personality/persona_compiler.py` | Identity/task/legacy behavior prompt compilation |

## Integrations

| File | Responsibility |
| --- | --- |
| `ai_bridge/tool_control.py` | Managed HTTP/MCP schemas, discovery, approvals and hot runtime |
| `ai_bridge/mcp_broker.py` | Existing hardened MCP broker/sanitization |
| `ai_bridge/openwa_integration.py` | Current-caller WhatsApp messaging/events/confirmation |
| `ai_bridge/web_research.py` | Bing/DDG discovery, static reader, Crawl4AI fallback |
| `ai_bridge/frappe_integration.py` | Caller-bound CRM/ERP/Helpdesk Realtime tools |
| `integrations/business_suite/phoneagent_frappe/phoneagent_frappe/api.py` | Frappe trust-boundary methods |
| `.../setup.py` | Frappe roles, custom fields and integration user provisioning |
| `integrations/business_suite/compose.yaml` | Full business/OpenWA/research service topology |
| `integrations/business_suite/frappe.Containerfile` | Custom immutable Frappe image |

## Studio, security and operations

| File | Responsibility |
| --- | --- |
| `ai_bridge/web_static/index.html` | Complete Studio UI/CSS/JavaScript |
| `ai_bridge/production_security.py` | Dial policy, redaction and chained audit ledger |
| `ai_bridge/secure_storage.py` | Mode-0600 atomic private file primitives |
| `ai_bridge/call_recording.py` | Consent-gated recording and retention |
| `qualification/device_qualification.py` | Evidence-backed Android profile qualification |
| `qualification/devices/*.json` | Qualified hardware/build profiles |
| `tools/install_macos.sh` | Transactional native installer/rollback staging |
| `tools/install_full_business_suite_macos.sh` | One-command complete product install |
| `tools/backup_business_suite.sh` | Frappe backup export |
| `tools/restore_business_suite.sh` | Confirmed restore with recovery point |
| `release/frozen-whatsapp.sha256` | Frozen direct/Android WhatsApp source manifest |
| `release/build_release.sh` | Release, SBOM, audit, signing and manifest |

## Documentation routing

- Beginner full UI: `docs/WEBUI_USER_GUIDE.md`
- Identity: `docs/IDENTITY_KERNEL.md`
- Persona/tasks/Realtime: `docs/PERSONA_AND_TASK_GUIDE.md`
- Call context: `docs/CALL_CONTEXT_STRATEGY.md`
- Generic tools/MCP: `docs/TOOLS_AND_MCP.md`
- OpenWA: `docs/OPENWA_INTEGRATION.md`
- Web research: `docs/WEB_RESEARCH.md`
- Business suite: `docs/BUSINESS_SUITE.md`
- External agents: `docs/EXTERNAL_AGENT_CONTROL_PLANE.md`
- Security/operations: `docs/SECURITY_AND_OPERATIONS.md`
- New Mac install: `docs/NEW_MAC_INSTALL_GUIDE.md`

## Search recipes

```bash
# HTTP/MCP surface
rg -n "router.add|TOOLS =|RESOURCES =|handle_(get|post)_" ai_bridge

# Config fields and strict schemas
rg -n "class .*BaseModel|Field\(|Literal\[|CONFIG_FIELDS" ai_bridge

# Call event producer/consumer
rg -n 'PHONE_AGENT_EVENT|_emit_event|broadcast|tool_call|call_completion' ai_bridge

# Android command or audio status field
rg -n 'command|audio|capture|injection|generation|sequence' android_service_apk/src mac_client

# Exact tool implementation and permission path
rg -n 'tool_name|allowed_tools|approval_mode|task_ids' ai_bridge integrations

# Tests covering a component
rg -n 'ComponentName|event_name|tool_name' tests
```

Do not search generated Rust `target/`, installed runtimes, backups or customer logs unless the task
specifically needs runtime evidence.
