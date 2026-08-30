# Architecture and Trust Boundaries

## Product shape

PhoneAgent turns a qualified Android phone and a Mac into an AI telephone operator. The Mac owns AI
reasoning, conversation policy and orchestration. Android owns Telecom call state and the privileged
telephony audio route. Docker owns business applications and optional sidecars. Only one live voice
host owns the phone at a time.

## Five planes

### 1. Protected device and media plane

- `android_service_apk/` is the privileged Android gateway.
- `PhoneAgentInCallService` observes/answers Telecom calls and mutes the physical microphone path.
- `CallManager` owns dial, answer, hold and hang-up state.
- `DigitalAudioBridge` captures remote audio and injects AI audio into the telephony uplink.
- `ProtocolControlServer`, `ProtocolCodec` and `LinkSessionRegistry` provide authenticated,
  replay-resistant control.
- `VoipAudioRoute` is the Android WhatsApp/VoIP audio route and is frozen.
- ADB forwards ports 8765–8768 from the Mac to Android.

The network audio format is 16 kHz, mono, signed 16-bit PCM. Android's telephony `AudioTrack` is the
authoritative playout clock. Do not move this plane into Docker Desktop.

### 2. Native per-call AI plane

- `PhoneAgentWebServer` is the persistent Studio/control process on port 8090.
- It starts one `phone-agent-voice` child for outbound calls or one persistent inbound receptionist.
- `PhoneVoiceAgent` owns the authenticated phone link, one `CallSessionState`, one transport and one
  AI pipeline per call.
- `VoiceHostLock` prevents two voice hosts from owning the hardware.
- `PhoneAgentTransport` converts authenticated framed media into Pipecat frames and sends generated
  PCM back under generation/credit control.
- `OpenAIRealtimeWebSocketPipeline` is the qualified direct Realtime PCM path.
- `ChatGPTRealtimePipeline` is the WebRTC compatibility path.
- `ProductionCallPipeline` is the cascade STT → LLM → TTS alternative.

### 3. Behavior and policy plane

- Identity Kernel: constitution, voice identity, examples, evaluation, versioning, skills and
  approved memory.
- Task Engine: objective, opening, knowledge, slots, strategy, objections, allowed tools, success
  and stop conditions.
- `AgentPolicyRuntime`: compiles identity, task, call direction and live state into model context;
  observes authoritative transcripts; evaluates turns; enforces continuity and spoken policy.
- `CallContextPolicy`: distinguishes outbound cold prospecting from inbound caller intent and delays
  qualification until it is conversationally appropriate.
- `ToolArgumentGrounding`: preserves caller-dictated text before durable writes.
- `CallPolicy` and `AuditLedger`: destination normalization, rate/cooldown limits, redacted audit and
  consent boundaries.

### 4. Tool and business plane

- Managed HTTP, stdio MCP and Streamable HTTP MCP tools.
- Purpose-built OpenWA current-caller messaging.
- Purpose-built Bing/Fast Reader/Crawl4AI research.
- Caller-bound Frappe CRM, ERPNext and Helpdesk tools.
- Frappe campaign scheduler and durable business records.

These integrations are not allowed to become part of the audio-critical loop. Slow tools require a
brief spoken preamble and bounded execution.

### 5. External-agent control plane

- Local authenticated REST under `/api/control/*`.
- Local stdio MCP executable `phone-agent-mcp`.
- One versioned `AgentPackage` across identity, task, skills, mutable memory, runtime, tools,
  OpenWA, web research and business configuration.
- Dry-run validation, stale-write protection, staged deployment, atomic effective activation,
  bounded events and rollback.

This plane can change declarative behavior and operate calls. It cannot edit source code, Android
routing, media, caller binding, secret redaction, audit integrity or hardware locks.

## Native and container boundary

The complete business product is one Compose application, not one literal container. Frappe needs
database, cache, queue, workers, scheduler, WebSocket and frontend processes. The Compose stack also
contains OpenWA and Crawl4AI. PhoneAgent remains native because Docker Desktop cannot reliably own
the qualified USB/Android audio path.

Default loopback services:

| Port | Owner | Purpose |
| --- | --- | --- |
| 8090 | Native Studio | UI, REST, WebSocket, control plane |
| 8765 | Android via ADB | gateway health/legacy HTTP control |
| 8766 | Android via ADB | remote caller PCM toward Mac |
| 8767 | Android via ADB | AI PCM toward telephony uplink |
| 8768 | Android via ADB | authenticated framed control |
| 8080 | Compose frontend | ERPNext, Frappe CRM and Helpdesk |
| 2785 | OpenWA | dashboard/API and linked WhatsApp session |
| 11235 | Crawl4AI | JavaScript page fallback |

Database and Redis ports are not published to the Mac network.

## Persistence

- `~/.config/phone-agent/studio.json`: selected runtime/task/channel settings.
- `~/.config/phone-agent/identity/`: active identity, revisions, history, skills, trust and blocks.
- `~/.config/phone-agent/tasks/`: user-authored task contracts.
- `~/.config/phone-agent/tools.json`: managed tool connections.
- `~/.config/phone-agent/openwa.json`: masked-in-UI OpenWA operator configuration.
- `~/.config/phone-agent/web-research.json`: live research configuration.
- `~/.config/phone-agent/frappe.json`: business integration configuration.
- `~/.config/phone-agent/control-plane/`: AgentPackage deployments and active pointer.
- `~/.local/share/phone-agent/`: caller memory, direct WhatsApp session, qualification, recordings,
  runtime, audit and backups.
- Docker named volumes: Frappe sites/database/queues and OpenWA linked-session data.

## Source-of-truth order

1. Running backend/device state for operational truth.
2. Strict Python/Pydantic/Java protocol schemas for accepted configuration and wire behavior.
3. Active files under the user configuration directory.
4. Repository documentation for intent and operating guidance.
5. Studio display text; it is a view, not the authority.

## Security model

- Studio binds to loopback and rejects hostile Host/Origin mutations.
- MCP control uses a private mode-0600 bearer token hidden inside the stdio process.
- Remote tool endpoints require HTTPS unless an explicit loopback/insecure exception is allowed.
- Redirects, unknown fields, schema drift, oversized output and unbounded time fail closed.
- Phone/email/secret material is redacted from model-facing and audit-facing output where required.
- Containers are digest-pinned, resource-bounded and use persistent volumes intentionally.
- OpenWA is unofficial and retains WhatsApp account-enforcement risk.
