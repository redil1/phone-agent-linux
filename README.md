# PhoneAgent Gateway

PhoneAgent connects a rooted Android phone call to a Pipecat voice pipeline on the Mac. The
runtime combines speech recognition, an LLM, streaming speech synthesis, a configurable agent
persona, task contracts, caller memory, deterministic permission checks, and per-turn evaluation.

## Install on a new Mac

```bash
git clone https://github.com/redil1/PhoneAgent.git
cd PhoneAgent
./tools/bootstrap_macos.sh
```

Installs the locked environment, runs lint and the full suite, builds the macOS
app and installs the Studio service. Requires Apple Silicon: Kokoro and Parakeet
run on MLX/Metal, which has no Intel build. See [`SETUP.md`](SETUP.md) for the
phone, link key and provider steps a clone cannot carry with it.

## Run

Already set up? Start the local Studio directly:

```bash
uv sync
uv run phone-agent-web
```

Open `http://127.0.0.1:8090`. Configure the persona, active task, call-specific instructions,
providers, and voice before starting a call. Saved persona and task settings apply to the next call.

The Studio has two independent latency controls. **Low-latency conversation mode** prepares an
exact-match response during the caller's final pause. **Instant conversational reactions** plays a
short cached acknowledgement after the caller turn is confirmed and while the real answer is
generated. The reaction is never added to conversation context, memory, tools, or task state.
Both controls fail open to the standard response path and can be disabled for the next call.

Live transcription separates provisional recognition from authoritative caller turns. Interim
text may prefetch an answer, but it cannot enter the transcript, LLM context, speech, or memory.
Final English/French turns keep the low-latency endpoint; uncertain language switches and
non-final hypotheses receive additional revision time. Corrections arriving without a new
acoustic speech epoch replace or suppress the earlier hypothesis instead of creating a second
caller turn.

Supertonic 3 is the primary local TTS experiment. It is prewarmed before calls, renders through one
serialized local worker, and is converted once to 16 kHz mono PCM for the phone. If local synthesis
fails, the same reply is rendered by the Andrew Multilingual Edge voice so the call remains alive.
Supertonic 2 is selectable in the Studio for the maximum-speed comparison.

Phone playout uses one clock: Android's telephony `AudioTrack`. The Mac sends exact 20 ms PCM
frames into a bounded 100 ms startup reservoir, and Android acknowledges frames as its playout
writer consumes them. A 12-frame credit window prevents unbounded TCP buffering, while explicit
end-of-speech markers stop queue-empty guesses from splitting one sentence into multiple segments.
Interruption advances a generation and flushes both the phone queue and `AudioTrack`, so cancelled
speech cannot re-enter playback. `/audio/status` exposes starvation, concealment, queue-depth,
acknowledgement, and underrun counters for call-quality diagnosis.

Useful experiment overrides:

```bash
# Primary quality profile (the default)
PHONE_AGENT_TTS_PROVIDER=supertonic PHONE_AGENT_TTS_MODEL=supertonic-3 \
  PHONE_AGENT_TTS_VOICE=M1 PHONE_AGENT_SUPERTONIC_STEPS=8 uv run phone-agent-web

# Maximum-speed profile
PHONE_AGENT_TTS_PROVIDER=supertonic PHONE_AGENT_TTS_MODEL=supertonic-2 \
  PHONE_AGENT_TTS_VOICE=M1 PHONE_AGENT_SUPERTONIC_STEPS=5 uv run phone-agent-web

# Reproduce the English/French local benchmark and write phone-ready WAV files
uv run python -m phone_agent_gateway.mac_client.supertonic_benchmark
```

Set `PHONE_AGENT_SUPERTONIC_FALLBACK_TO_EDGE=false` only when testing a strict local-only failure
mode. Model files are downloaded once and retained in the normal Supertonic cache.

The direct voice entry point is:

```bash
uv run phone-agent-voice --dial PHONE_NUMBER
```

## Direct WhatsApp calls

The free direct WhatsApp channel runs through a separate Rust sidecar based on
`whatsapp-rust`; it does not use or modify the Android/GSM path.

```bash
cd whatsapp_channel/rust_caller
./build.sh
cd ../..
./whatsapp_channel/whatsapp-rust-caller pair-phone YOUR_NUMBER --country-code 212
PHONE_AGENT_CALL_CHANNEL=whatsapp uv run phone-agent-voice --dial TARGET_NUMBER
```

The sidecar stores its linked-device session under
`~/.local/share/phone-agent/whatsapp-rust.db`, waits for the peer's WhatsApp
accept event before greeting, streams two-way 16 kHz PCM, and uses
generation-aware source flushing for barge-in. See
`whatsapp_channel/README.md` for its protocol, verification, and operational
limits.

Runtime options are documented in `.env.example`. Caller memory is enabled by default and stored
under `~/.local/share/phone-agent/`; the editable persona is stored under
`~/.config/phone-agent/`. Studio settings are also retained there between launches.

The versioned Identity Kernel adds an immutable constitution, reviewed memory blocks, trusted
progressive skills, asynchronous Graphiti mirroring, deterministic and live Realtime behavioral
evaluation, exact-hash approval, and next-call activation without changing any call transport.
See [`docs/IDENTITY_KERNEL.md`](docs/IDENTITY_KERNEL.md).

## Verify

```bash
uv run pytest -q
uv build
```

The hardware-marked tests are excluded by default because they require a connected phone and may
change live call state.

## Production operations

Install the tested macOS app and local service with `tools/install_macos.sh`; restore the previous
installation with `tools/rollback_macos.sh`. Run `uv run phone-agent-qualify --ensure-forwards` to
produce a formal report for the connected Android device. The local stdio MCP entry point is
`uv run phone-agent-mcp`. Dial requests made through MCP require an exact one-time approval in the
Studio before execution.

Studio 0.7 includes a **Tools & MCP** workspace for declarative HTTP tools, local stdio MCP servers
and remote Streamable HTTP MCP servers. Connections and individual tools have separate activation,
task assignment and optional per-use approval. Reviewed changes hot-reload into active OpenAI
Realtime WebSocket or WebRTC calls without touching GSM or WhatsApp transports. See
[`docs/TOOLS_AND_MCP.md`](docs/TOOLS_AND_MCP.md).

The same workspace now includes purpose-built live web research. The Realtime agent announces a
brief wait, discovers links through Bing with a DuckDuckGo backup, reads pages through a fast
Trafilatura path, and uses a digest-pinned Crawl4AI browser only for JavaScript fallbacks. Results
carry bounded provider-labelled evidence and latency; the Realtime AI evaluates relevance,
freshness, credibility, confidence and the appropriate next action. Install the
optional browser fallback with `tools/install_crawl4ai_sidecar.sh`. See
[`docs/WEB_RESEARCH.md`](docs/WEB_RESEARCH.md).

The workspace also supports an isolated OpenWA messaging companion. During a live call, activated
Realtime tools can interact only with the current caller's confirmed WhatsApp chat; live incoming
messages and delivery updates can re-enter the spoken conversation. It does not carry call audio or
modify GSM or the frozen direct WhatsApp voice pipeline. See
[`docs/OPENWA_INTEGRATION.md`](docs/OPENWA_INTEGRATION.md).

The installed LaunchAgent uses a self-contained runtime under
`~/.local/share/phone-agent/runtime` so macOS does not need to grant a background service access to
the development checkout on Desktop. Installation is health-checked and automatically restores
the previous snapshot if the new service cannot start.

Building, installing and pairing the Android gateway APK is documented in
[`docs/ANDROID_APP.md`](docs/ANDROID_APP.md).

Security policy, consent-aware recordings, MCP trust boundaries, release signing/SBOM generation,
backups, rollback, and device qualification are documented in
`docs/SECURITY_AND_OPERATIONS.md`.

For a beginner-friendly explanation of every PhoneAgent Studio field, button, status and safe
workflow, see [`docs/WEBUI_USER_GUIDE.md`](docs/WEBUI_USER_GUIDE.md).

Automatic outbound cold-prospecting versus inbound intent-led behavior is documented in
[`docs/CALL_CONTEXT_STRATEGY.md`](docs/CALL_CONTEXT_STRATEGY.md).

Enable **AI answers incoming GSM calls** in the Live Call workspace to keep the authenticated AI
receptionist listening while Studio is idle. It pauses for an outbound call and restarts afterward.

## CRM, customer service and ERP

Install the complete local business stack with one command:

```bash
./tools/install_full_business_suite_macos.sh
```

It adds ERPNext, Frappe CRM, Frappe Helpdesk, autonomous consent-aware campaigns, caller-bound live
AI business tools, persistent Docker storage, backups and restore while preserving the native
qualified GSM and frozen direct WhatsApp media paths. See
[`docs/BUSINESS_SUITE.md`](docs/BUSINESS_SUITE.md).

For a beginner-friendly clean-Mac installation, pairing and end-to-end testing walkthrough, use
[`docs/NEW_MAC_INSTALL_GUIDE.md`](docs/NEW_MAC_INSTALL_GUIDE.md).

External agents such as Codex and Hermes can configure and operate PhoneAgent through the versioned
AgentPackage MCP/REST control plane without changing framework or media code. See
[`docs/EXTERNAL_AGENT_CONTROL_PLANE.md`](docs/EXTERNAL_AGENT_CONTROL_PLANE.md).

For complete Hermes Agent installation, MCP filtering, master-skill setup, deployment, calling,
monitoring and rollback, see
[`docs/HERMES_PHONEAGENT_SETUP.md`](docs/HERMES_PHONEAGENT_SETUP.md).

For agents performing engineering, configuration, diagnosis or operations on the framework itself,
use the reusable [`phoneagent-master` skill](skills/phoneagent-master/SKILL.md).
