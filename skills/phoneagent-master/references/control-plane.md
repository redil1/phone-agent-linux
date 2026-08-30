# External-Agent Control Plane

## Purpose

Codex, Hermes or another compatible agent should act as PhoneAgent's administrator/orchestrator,
while PhoneAgent remains the protected call executor. The external agent may design, validate,
deploy, operate, observe and roll back declarative behavior. It may not alter framework/media code.

## AgentPackage

`AgentPackage` schema version 1 is the atomic desired-state unit. It contains:

- package ID, display name, objective and labels;
- complete `IdentityProfile`;
- complete task contract;
- `RuntimeControl`;
- user `SkillDraft` objects needed by the identity;
- full replacement set of mutable approved memory blocks;
- managed tool config;
- OpenWA config;
- web-research config;
- Frappe business config.

Nested credentials returned by the active-package API are masked. Sending the mask back preserves the
stored secret. The package transport is bounded to 400,000 characters.

## Safe deployment lifecycle

1. Read `phoneagent://schema/agent-package`.
2. Read `phoneagent://state/active-package` or call `phone_agent_get_active_package`.
3. Clone the full package and modify only desired fields.
4. Call `phone_agent_validate_package`.
5. Resolve critical failures; inspect warnings such as task tools not active in the package.
6. Call `phone_agent_stage_package` with a meaningful reason and external-agent identity.
7. Record the deployment ID and exact package hash.
8. Call `phone_agent_activate_deployment`.
9. Read back the active package/deployment/effective state.
10. Operate calls and follow `phone_agent_recent_events` with a sequence cursor.
11. Use `phone_agent_rollback_deployment` if qualification regresses.

Staging never changes active behavior. Activation revalidates the exact hash and compares the staged
base-state hash with the current effective state. A manual/other-agent change after staging forces a
new stage rather than silently overwriting it.

## Activation semantics

- Refused during a call.
- Serialized by an activation lock.
- Snapshots effective private config files before writes.
- Validates task, runtime, tools, OpenWA, research, business, memory, skills and identity.
- Saves/trusts package user skills under the authenticated actor.
- Replaces the mutable memory set while retaining immutable self memory.
- Executes identity revision → evaluation → approval → activation.
- Marks previous deployment superseded and writes a private active pointer.
- Restores snapshots and marks the deployment failed if effective activation raises.

Identity revision/audit artifacts may remain as evidence after a failed activation; the effective
active identity/config is restored.

## MCP resources

- `phoneagent://schema/agent-package`
- `phoneagent://state/active-package`
- `phoneagent://state/capabilities`

## MCP tools

Hermes exposes these as `mcp__phoneagent__<original_name>`. Hermes must call those registered tools
directly. It must not use `mcporter`, shell, Python scripts or raw REST as a convenience layer when
the native tools are healthy. Read [hermes-native-mcp.md](hermes-native-mcp.md) for exact response
shapes and operation recipes.

Inspection:

- `phone_agent_status`
- `phone_agent_capabilities`
- `phone_agent_identity`
- `phone_agent_control_schema`
- `phone_agent_get_active_package`
- `phone_agent_validate_package`
- `phone_agent_list_deployments`
- `phone_agent_recent_events`
- `phone_agent_list_tasks`

Deployment:

- `phone_agent_stage_package`
- `phone_agent_activate_deployment`
- `phone_agent_rollback_deployment`

Operation:

- `phone_agent_dial`
- `phone_agent_hangup`
- legacy `phone_agent_request_dial` and `phone_agent_execute_approved_dial`

The admin dial tool treats possession of the private local control token as operator authority, but
still applies destination normalization, rate/cooldown, consent, hardware preflight and one-call
locking.

Every tool result keeps its endpoint envelope. Do not assume nested values are flattened. For
example, package validation is `result.validation.valid`, staging returns
`result.deployment.deployment_id`, and activation returns `result.deployment.state`.

## REST API

Authenticated endpoints:

```text
GET  /api/control/schema
GET  /api/control/package
GET  /api/control/deployments
GET  /api/control/events?after=SEQUENCE&limit=1..200
POST /api/control/validate
POST /api/control/stage
POST /api/control/activate
POST /api/control/rollback
POST /api/control/dial
POST /api/control/hangup
```

REST uses the private mode-0600 bearer token at `~/.config/phone-agent/control.token`. Prefer stdio
MCP so an external model never receives the token.

## Codex connection

The installed command is:

```text
~/.local/share/phone-agent/runtime/.venv/bin/phone-agent-mcp
```

Register it with current Codex CLI:

```bash
codex mcp add phoneagent -- \
  "$HOME/.local/share/phone-agent/runtime/.venv/bin/phone-agent-mcp"
codex mcp get phoneagent
```

Existing Codex tasks may need a new task/restart to discover a newly registered server.

For another MCP client, configure the same executable as a local stdio server.

## Event cursor

PhoneAgent retains a bounded in-memory window of recent Studio/call events. Each receives a sequence
and observation time. Top-level caller routing identifiers are hashed. Transcript/tool content remains
administrator-visible because orchestration needs it; treat it as customer data.

Poll with `after` equal to the last `next_after`. Do not busy-loop; callers and tools naturally create
gaps. Event history is operational, not a durable CRM record.

## External-agent prompt pattern

A strong request to the administrator agent has this shape:

```text
Read the PhoneAgent schema and active package. Build a package for <objective> and <audience>.
Preserve protected media and security boundaries. Configure identity, task, knowledge, skills,
tools, CRM behavior, voice/language/channel and evaluation examples. Validate it, explain the
effective diff, stage it, then activate only if validation is contract-clean. Monitor the call
and report only backend-verified results.
```

The external agent must not construct a package from memory. Clone the current package so masked
credentials, existing integrations and complete strict fields remain intact.

## Conflict and recovery rules

- A 409/stale-state response means read current state, reapply intended changes and stage again.
- A failed deployment is not reusable; create a new stage after correcting it.
- Rollback creates a new deployment from the historical package; it does not rewrite history.
- Do not delete deployment files manually.
- Do not edit active identity/config files behind the control plane while another agent is staging.
- Use native installer rollback for a code/runtime release failure, AgentPackage rollback for behavior
  regression, and business-suite restore for database recovery.
- A one-call channel switch is currently a persistent AgentPackage change. Record the previous
  deployment and restore it after the call when the operator intended the switch to be temporary.

## Deliberately unavailable powers

AgentPackage has no shell command, file path, source patch, Docker mutation, ADB command, raw secret,
codec, PCM frame, Android mixer or frozen-manifest field. If a job appears to require one, determine
whether it is actually a framework defect requiring a separate authorized engineering change.
