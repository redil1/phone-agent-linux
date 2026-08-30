# Installation, Operations, Testing and Quality

## Prerequisites

- macOS with Docker Desktop running;
- Xcode Command Line Tools and `uv`;
- ADB/Android platform tools for GSM;
- qualified rooted Android phone and data cable;
- authenticated Codex/OpenAI environment for the configured Realtime path;
- Rustup only when an Intel Mac must build the frozen-source direct WhatsApp sidecar.

## Installation

Complete product:

```bash
./tools/install_full_business_suite_macos.sh
```

Native app/runtime only:

```bash
./tools/install_macos.sh
```

The native installer synchronizes locked dependencies, verifies frozen WhatsApp, lints/tests, builds
and signs the Mac app, builds a self-contained wheel/runtime, stages a LaunchAgent, activates it,
health-checks port 8090 and restores the previous snapshot if activation fails.

The full installer additionally creates private business credentials, builds the custom Frappe
image, starts the Compose product, migrates/provisions the site, preserves the OpenWA volume,
configures business/research tools and then runs the native installer.

Never use `sudo` for the user-scoped installation.

## Status and service URLs

```bash
./tools/business_suite_status.sh
adb devices -l
curl -fsS http://127.0.0.1:8090/api/status
```

- Studio: `http://127.0.0.1:8090/`
- CRM/ERP/Helpdesk: `http://127.0.0.1:8080/`
- OpenWA: `http://127.0.0.1:2785/`
- Crawl4AI: `http://127.0.0.1:11235/health`

Do not print secret files in automated logs.

## Device qualification

```bash
uv run phone-agent-qualify --ensure-forwards
```

Qualification requires exactly one authorized Android device and checks the declared device/model,
SDK, ABI, build fingerprint, root, privileged/system package flags, dialer role, gateway/link key,
audio permissions, telephony route, network format, authenticated protocol and audio-service state.

Idle qualification proves installation/control readiness. It does not prove a remote person heard
the AI. That requires an authorized physical call.

Do not apply a qualification profile to another Android model/build without a new qualification
project.

## Test tiers

### Tier 1: static and unit

```bash
uv run ruff check ai_bridge integrations/business_suite/phoneagent_frappe \
  mac_client qualification release tests tools
uv run python -m compileall -q ai_bridge integrations/business_suite/phoneagent_frappe
uv run python tools/verify_frozen_whatsapp.py
uv run pytest -q
```

Run JavaScript syntax checks when Studio changes, Bash syntax for installers/operations, JSON parsing
for manifests and `docker compose config --quiet` for Compose changes.

### Tier 2: installed local runtime

- LaunchAgent state is running and points to the self-contained runtime.
- `/api/status` is healthy/IDLE.
- Inbound receptionist is listening when enabled.
- ADB device state is `device` when GSM is in scope.
- Frappe required apps, OpenWA session and Crawl4AI are ready.
- Installed `phone-agent-mcp` lists expected tools/resources.
- Active AgentPackage read-back matches the deployment.

### Tier 3: integration behavior

- Validate/stage/activate/read-back a cloned AgentPackage.
- Prove stale-write activation rejection.
- Prove rollback to a previous package and restoration of the preferred package.
- Invoke caller-bound Frappe context through the real runtime.
- Run a real web-research query and inspect sources/warnings.
- Test OpenWA health/session without sending unless messaging is explicitly authorized.

### Tier 4: authorized physical call

Test at least:

- inbound GSM auto-answer;
- outbound GSM;
- direct WhatsApp voice;
- English and French;
- interruption/barge-in;
- current caller number;
- live research with freshness/source demand;
- durable CRM/Helpdesk action and backend verification;
- WhatsApp message and exact confirmation state;
- AI `end_call` and physical disconnect.

Repeat representative calls; one success does not establish reliability.

## Logs and evidence

- `~/phone-agent-logs/studio.out.log`
- `~/phone-agent-logs/studio.err.log`
- `~/phone-agent-logs/voice-host-raw.log`
- `~/.local/share/phone-agent/audit.jsonl`
- `~/.local/share/phone-agent/qualification/`
- `~/.local/share/phone-agent/recordings/` when authorized
- Frappe Call Log and business documents
- control-plane deployment records under `~/.config/phone-agent/control-plane/`

Prefer structured `PHONE_AGENT_EVENT` lines. Correlate by call ID; avoid dumping entire logs because
they can contain transcripts and exact caller identifiers.

## Common diagnosis routes

### Incoming call rings indefinitely

Verify ADB authorization, gateway health, dialer role, auto-answer, voice lock, child process and
inbound `listening`. A stale UI label is not enough.

### AI transcript appears but caller hears nothing

Inspect Android injection route, TX connection, playback acknowledgements, queue/credits, underruns,
generation and `call_error`. PhoneAgent should fail/hang up if the telephony uplink is unavailable.

### False caller speech or greeting interruption

Inspect startup verifier, echo correlation/suppression, acoustic epoch, transcript item IDs,
generation changes and playback overlap. Do not accept a transcript line as proof the caller spoke.

### Slow opening only

Separate child/model import, Realtime connection, call acceptance, media attach and first-audio time.
Preload/preconnect outside the active audio path; do not loosen turn or playback correctness.

### Tool action is wrong

Compare caller transcript, model tool arguments, grounding event, tool result and backend record. Add
a regression at the earliest layer that changed the meaning.

### AI does not end call

Check offered `end_call` tool, tool call/result, terminal response, playback completion and completion
sink. Do not replace model-owned ending with a broad goodbye regex.

### External package will not activate

Read validation checks, deployment state and effective-state hash. A call, stale config, masked-secret
loss, missing trusted skill, invalid task/provider combination or critical identity contract can
block. Clone current package and restage; do not edit deployment JSON.

## Backup and restore

```bash
./tools/backup_business_suite.sh
./tools/restore_business_suite.sh /absolute/backup/directory --confirm
./tools/rollback_macos.sh
```

- Business backup covers Frappe database/files and relevant environment evidence.
- AgentPackage rollback restores behavior configuration.
- Native rollback restores the previous installed app/runtime/LaunchAgent.
- OpenWA linked-device data is in its named Docker volume; ordinary stop does not unlink it.

Never test restore against production data casually. Restore is destructive and requires explicit
`--confirm`; it first creates a recovery point.

## Release

`release/build_release.sh` requires a clean committed tree, signing identity unless `--unsigned` is
explicitly used for local test artifacts, frozen verification, full lint/tests, SBOM, dependency
audit, app build/signing, device profiles, docs, manifest and checksums.

Do not claim a release artifact exists when the repository is entirely untracked/dirty and the
release script correctly refuses it.

## Production readiness statement

Report readiness within:

- the qualified Android profile;
- the tested Mac architecture/version;
- current OpenAI/third-party availability;
- linked WhatsApp accounts;
- configured business data and legal operating policy.

OpenWA/WhatsApp changes, carriers, external websites and provider APIs remain outside absolute
control.
