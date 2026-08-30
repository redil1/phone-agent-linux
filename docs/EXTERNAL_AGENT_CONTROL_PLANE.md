# PhoneAgent External-Agent Control Plane

PhoneAgent exposes a local authenticated MCP server and REST control plane for Codex, Hermes and
other compatible agents. The external agent configures declarative behavior; it cannot edit
framework code, Android routing, GSM media or frozen WhatsApp media.

## Architecture

One versioned `AgentPackage` contains:

- the complete Identity Kernel profile and behavioral examples;
- one complete task contract, knowledge, strategy, objections and success criteria;
- trusted progressive skill drafts;
- mutable approved memory blocks;
- validated voice, Realtime, language, channel and latency parameters;
- managed HTTP/MCP tools and task allowlists;
- OpenWA messaging configuration;
- live web-research configuration;
- Frappe CRM, ERP and Helpdesk configuration.

Credentials are always returned masked. A package can preserve an existing masked credential but
cannot retrieve the secret value.

## Connect Codex

After PhoneAgent is installed, register the local stdio server:

```bash
codex mcp add phoneagent -- \
  "$HOME/.local/share/phone-agent/runtime/.venv/bin/phone-agent-mcp"
```

Verify it:

```bash
codex mcp get phoneagent
```

For another MCP client, configure the same executable as a local stdio MCP server. The process
loads the private local control token itself; never place that token in the external agent prompt.

For the complete Hermes-specific installation, skill setup, filtering, qualification and operating
workflow, see `docs/HERMES_PHONEAGENT_SETUP.md`.

## Safe customization workflow

An external agent should always use this sequence:

1. Read `phoneagent://schema/agent-package`.
2. Call `phone_agent_get_active_package`.
3. Clone the returned package and change only parameters needed for the requested job.
4. Call `phone_agent_validate_package` as a dry run.
5. Resolve every contract-critical error and review warnings.
6. Call `phone_agent_stage_package` with an auditable reason and agent identity.
7. Call `phone_agent_activate_deployment` using the returned deployment ID.
8. Read the active package again and compare the effective-state hash.
9. Call `phone_agent_dial`, or leave inbound auto-answer listening.
10. Follow the call through `phone_agent_recent_events`.
11. Read the final CRM/call outcome through the configured business tools.
12. Roll back to a prior deployment if qualification or production quality regresses.

Staging never changes active behavior. Activation is refused while a call is active or when any
configuration changed after staging. This prevents partial and stale external-agent writes.

## MCP resources

- `phoneagent://schema/agent-package`
- `phoneagent://state/active-package`
- `phoneagent://state/capabilities`

## MCP tools

Read-only inspection:

- `phone_agent_status`
- `phone_agent_capabilities`
- `phone_agent_identity`
- `phone_agent_control_schema`
- `phone_agent_get_active_package`
- `phone_agent_validate_package`
- `phone_agent_list_deployments`
- `phone_agent_recent_events`
- `phone_agent_list_tasks`

Configuration and deployment:

- `phone_agent_stage_package`
- `phone_agent_activate_deployment`
- `phone_agent_rollback_deployment`

Calling:

- `phone_agent_dial`
- `phone_agent_hangup`
- the legacy one-time approval pair `phone_agent_request_dial` and
  `phone_agent_execute_approved_dial`

The administrator-scoped `phone_agent_dial` still passes PhoneAgent's destination normalization,
rate, cooldown, hardware, recording-consent and one-call checks. It does not bypass those policies.

## REST endpoints

All `/api/control/*` endpoints require the private bearer token stored at
`~/.config/phone-agent/control.token`. Prefer MCP so the external agent never sees this value.

- `GET /api/control/schema`
- `GET /api/control/package`
- `GET /api/control/deployments`
- `GET /api/control/events?after=0&limit=100`
- `POST /api/control/validate`
- `POST /api/control/stage`
- `POST /api/control/activate`
- `POST /api/control/rollback`
- `POST /api/control/dial`
- `POST /api/control/hangup`

## Immutable boundaries

No AgentPackage or control operation can modify:

- GSM or WhatsApp media implementation;
- Android privileged routing;
- codecs, PCM framing or playback clocks;
- authenticated caller-number binding;
- secret redaction;
- recording-consent and do-not-call enforcement;
- the one-call hardware lock;
- audit-ledger integrity;
- application source code or Docker security boundaries.

## Recovery

Every deployment is retained under `~/.config/phone-agent/control-plane/deployments/`. Effective
configuration writes are snapshotted before activation and restored if activation fails. Previous
active packages remain available for explicit rollback. Normal business-suite backup and native
PhoneAgent rollback procedures remain unchanged.
