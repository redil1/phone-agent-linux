# PhoneAgent Security and Operations

PhoneAgent is a local telephony appliance. The Studio binds to `127.0.0.1`; the Android media
protocol is authenticated; private MCP servers run as bounded stdio subprocesses; and outbound
calls require a policy decision. The direct WhatsApp implementation is frozen by
`release/frozen-whatsapp.sha256` and is verified before installation, CI, and release.

## Installation and rollback

Run `tools/install_macos.sh`. It performs a locked dependency sync, frozen-pipeline check, lint,
the non-device test suite, native app compilation, code-signature verification, and LaunchAgent
installation. The service runs only on loopback. Existing app and LaunchAgent files are moved to
`~/.local/share/phone-agent/install-backups/` before replacement. Run
`tools/rollback_macos.sh` to restore that snapshot; the replaced installation remains recoverable.

The LaunchAgent executes a self-contained, wheel-installed runtime at
`~/.local/share/phone-agent/runtime`, not the development checkout. This avoids macOS Files &
Folders/TCC denying a background process access to a checkout stored on Desktop. The installer
stages and validates this runtime before stopping the old service, probes `/api/status` after
activation, and restores the previous app, runtime, and LaunchAgent automatically if activation
does not become healthy. Identity data, Studio settings, audit logs, and recordings remain in their
private user-scoped configuration and data directories, so upgrades do not overwrite them.

The local development build is ad-hoc signed. Distribution releases must set
`PHONE_AGENT_CODESIGN_IDENTITY`; notarization additionally uses `PHONE_AGENT_NOTARY_PROFILE`.
The release script refuses a dirty source tree and an unsigned distribution by default.

## Dial policy and audit

Copy `config/policy.example.json` to `~/.config/phone-agent/policy.json`. Unknown fields, invalid
numbers, emergency destinations, unapproved calls, denied or non-allowlisted numbers, premium
prefixes, excessive rate, cooldown violations, and calls over the duration bound fail closed.

Audit events are written as mode-`0600` hash-chained JSON Lines under
`~/.local/share/phone-agent/audit.jsonl`. Destinations are stored as a salted hash plus the last
four digits. The tail read and append are serialized and fsynced. Back up the ledger as an intact
file; removing or reordering records breaks the chain.

## Recordings and consent

Recording is off unless the operator checks the per-call consent control. The child call process
receives separate enablement and consent flags. Caller and agent PCM are observed only through the
generic transport, so recording cannot alter GSM or WhatsApp media code. Bounded callbacks enqueue
audio without disk I/O; a worker writes private `remote.wav`, `agent.wav`, and `conversation.wav`
files plus hashes and an outcome manifest. Dropped or missing audio makes the manifest incomplete.
Default retention is 30 days and deletes only expired, real child directories under the recording
root. Operators remain responsible for local notice, consent, retention, and deletion law.

Inbound GSM auto-answer runs with recording disabled. The receptionist cannot assume recording
consent merely because a caller initiated contact. An inbound recording workflow must establish any
required notice and consent before recording is separately enabled.

## MCP trust boundaries

`phone-agent-mcp` is a local stdio MCP server. It communicates with Studio using a mode-`0600`
bearer token and loopback HTTP. Read-only status calls execute directly. Dialing is two-phase:

1. MCP creates a request containing the exact normalized destination and recording choice.
2. Studio displays only its redacted identity to the operator.
3. The operator approves or rejects the exact request.
4. MCP may execute the approved request once before its five-minute expiry.

The Realtime MCP broker is a separate outbound boundary. It uses argv without a shell, a minimal
environment, schema and size bounds, short timeouts, secret/phone/email redaction, and a dual
allowlist: both local server configuration and the active task contract. Non-read-only third-party
MCP tools are never executed automatically.

Studio 0.7 also provides a managed Tools & MCP control plane. It supports declarative HTTP, local
stdio MCP and remote Streamable HTTP MCP connections. Connection activation, individual tool
activation and task assignment are independent gates. Tools may additionally require an exact,
expiring per-use operator approval. Configuration and approval files are private; browser-visible
headers are masked; HTTP redirects, unbounded schemas and unbounded outputs fail closed. Active
Realtime sessions hot-reload the reviewed catalog without restarting or modifying GSM or WhatsApp
media. See `docs/TOOLS_AND_MCP.md`.

The optional OpenWA messaging companion is a loopback-only, digest-pinned container with a private
master key, dedicated session-scoped PhoneAgent key, read-only root filesystem, bounded resources,
pacing, and persistent paired-session storage. Realtime receives only current-caller wrapper tools;
it cannot choose a recipient or access OpenWA credentials. Live message events are authenticated,
current-caller filtered, deduplicated, and injected as untrusted customer content. See
`docs/OPENWA_INTEGRATION.md` for pairing, operations, and the remaining account-ban risk of
unofficial WhatsApp automation.

The optional Crawl4AI research fallback is a separate, localhost-only, digest-pinned container.
It has a private bearer token, read-only root, dropped capabilities, no privilege escalation,
bounded CPU/memory/PIDs/queue/pages/depth/time, disabled hooks/webhooks and verified TLS. PhoneAgent
validates search and redirect destinations against public IP space before static retrieval, bounds
all evidence, and labels remote content untrusted. Search queries and source text are excluded from
the audit ledger.

The optional unified Business Suite uses ERPNext, Frappe CRM and Frappe Helpdesk as the durable
system of record. Its MariaDB/Redis services are internal-only, browser ports are loopback-bound,
credentials are private files, and Realtime receives caller-bound wrapper tools instead of generic
database access. Campaign claims require an administrator-activated campaign and are rechecked
against consent, do-not-call evidence, calling windows, daily limits, retry limits and the existing
PhoneAgent dial policy. AI-created quotations and sales orders remain drafts.

## Device qualification

Run `uv run phone-agent-qualify --ensure-forwards`. The report validates the device profile,
Android build, root, system/privileged gateway flags, default dialer role, gateway health,
authenticated protocol, key provision, telephony capture capability, exact PCM format, and audio
error counters. Reports contain a serial hash, evidence per check, and a report SHA-256. A passing
idle qualification proves installation and control readiness; it does not prove a remote party can
hear a live call. Live call tests remain explicit because they contact and may record a person.

## Release evidence

`release/build_release.sh` runs the complete non-device suite and emits wheels/sdist, the macOS app,
a locked CycloneDX 1.5 SBOM, device profiles, a release manifest, and `SHA256SUMS`. Use
`--unsigned` only for local validation. CI repeats the frozen guard, locked sync, lint, tests,
package build, SBOM parse, locked dependency vulnerability audit, Swift type-check, and app
signature verification. The lockfile excludes yanked `google-api-core==2.35.0`.

## Scope and residual risk

This project does not use the paid official WhatsApp Business calling API. Direct WhatsApp uses an
unofficial open-source client and may break when WhatsApp changes its private protocol or may expose
the linked account to enforcement. The freeze guard prevents accidental local regressions; it
cannot make an unofficial protocol supported by WhatsApp. GSM and WhatsApp live carrier tests
require a consenting recipient and therefore are never run silently by CI or the installer.

## External agent control over MCP

`phone-agent-mcp` is a local stdio MCP server. It communicates with Studio over
loopback HTTP using a mode-`0600` bearer token, so it cannot be driven from off
this machine.

It exposes 33 tools covering the whole appliance, because an agent that can only
dial cannot actually operate one: choosing the model, activating a tool,
configuring the CRM, editing the persona and attaching a handset are the job.

| Area | Tools |
|---|---|
| Calls | `dial`, `hangup`, `request_dial`, `execute_approved_dial` |
| State | `status`, `capabilities`, `identity`, `recent_events`, `get_evaluation`, `get_caller_memory` |
| Providers | `get_configuration`, `set_configuration` |
| Tools & MCP | `get_tool_control`, `set_tool_control` |
| Persona & tasks | `get_persona`, `set_persona`, `list_tasks`, `set_task`, `delete_task` |
| Business systems | `get_integration`, `set_integration`, `test_integration` (Frappe CRM/ERPNext, OpenWA, web research) |
| Handset | `set_remote_link`, `pairing_code` |
| Approvals | `list_approvals`, `decide_approval` |
| Deployment | `stage_package`, `validate_package`, `activate_deployment`, `rollback_deployment`, `list_deployments`, `get_active_package`, `control_schema` |

Every tool calls the same Studio endpoint the UI uses, so an external agent and
an operator cannot drift apart or bypass one another's guards, and every change
lands in the same audit ledger.

Two limits are deliberate and survive this breadth:

**Dialling still needs a person.** `phone_agent_request_dial` never places a
call. It creates a request that an operator approves in Studio, seeing only a
redacted destination; `phone_agent_execute_approved_dial` may then run it once,
within five minutes. Configuration is broad on purpose; calling a real human
being is not.

**Arguments are validated before any request leaves.** A malformed call changes
nothing, and the integration name is checked against a fixed set rather than
being interpolated into a URL, so it cannot be used to reach an arbitrary
endpoint.

Run it with `uv run phone-agent-mcp`.
