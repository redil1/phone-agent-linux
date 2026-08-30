# Hermes Native MCP Operation

Use this reference whenever Hermes operates PhoneAgent. The native tools registered in the Hermes
session are the primary and expected integration.

## Mandatory execution rule

When the `phoneagent` MCP server is connected and its tools are registered:

- call `mcp__phoneagent__...` tools directly;
- consume their structured result directly;
- perform multi-step workflows as sequential native tool calls;
- preserve deployment IDs, package hashes and event cursors in agent context;
- do not write a temporary Python/shell wrapper;
- do not launch `mcporter`;
- do not launch another `phone-agent-mcp` subprocess;
- do not call `/api/control/*` directly;
- do not read `~/.config/phone-agent/control.token`.

Using `mcporter` may reach the same server, but it creates a second client/process, bypasses Hermes'
native tool lifecycle and filtering, adds parsing mistakes and makes audit/debugging harder. It is not
the normal path.

## When a fallback is acceptable

A CLI or raw-protocol fallback is acceptable only when all are true:

1. the native `mcp__phoneagent__...` tools are absent or fail to register;
2. the user asked to diagnose/repair the connection;
3. `hermes mcp test phoneagent` and `/reload-mcp` did not restore it;
4. the fallback is read-only unless the user separately authorized a mutation;
5. Hermes reports the native-MCP defect rather than silently continuing forever through the fallback.

An upstream Hermes warning such as a stdio child-watcher coroutine warning is not by itself a reason
to abandon native MCP when tool calls and structured results still work. Report it and update/repair
Hermes separately.

## Native tool names

Hermes prefixes each original PhoneAgent tool name:

| Capability | Hermes native tool |
| --- | --- |
| Status | `mcp__phoneagent__phone_agent_status` |
| Capabilities | `mcp__phoneagent__phone_agent_capabilities` |
| Identity | `mcp__phoneagent__phone_agent_identity` |
| AgentPackage schema | `mcp__phoneagent__phone_agent_control_schema` |
| Active AgentPackage | `mcp__phoneagent__phone_agent_get_active_package` |
| Validate package | `mcp__phoneagent__phone_agent_validate_package` |
| Stage package | `mcp__phoneagent__phone_agent_stage_package` |
| List deployments | `mcp__phoneagent__phone_agent_list_deployments` |
| Activate deployment | `mcp__phoneagent__phone_agent_activate_deployment` |
| Roll back deployment | `mcp__phoneagent__phone_agent_rollback_deployment` |
| Recent events | `mcp__phoneagent__phone_agent_recent_events` |
| Task contracts | `mcp__phoneagent__phone_agent_list_tasks` |
| Administrator dial | `mcp__phoneagent__phone_agent_dial` |
| Hang up | `mcp__phoneagent__phone_agent_hangup` |
| Request gated dial | `mcp__phoneagent__phone_agent_request_dial` |
| Execute gated dial | `mcp__phoneagent__phone_agent_execute_approved_dial` |

Resource wrappers, when enabled by Hermes, are normally:

- `mcp__phoneagent__list_resources`
- `mcp__phoneagent__read_resource`

PhoneAgent resource URIs are:

- `phoneagent://schema/agent-package`
- `phoneagent://state/active-package`
- `phoneagent://state/capabilities`

## Result envelopes

Do not guess or flatten response fields.

### Status

```json
{
  "status": "ok",
  "call_state": "IDLE",
  "destination": "",
  "channel": "gsm",
  "task_id": "iptv",
  "identity_version": 10,
  "identity_hash": "sha256:..."
}
```

### Active package

```json
{
  "status": "ok",
  "package": {"schema_version": 1},
  "effective_state_hash": "sha256:...",
  "active_deployment_id": "dep_..."
}
```

Clone `result.package`; preserve masked values and all fields.

### Validation

```json
{
  "status": "ok",
  "validation": {
    "valid": true,
    "package_hash": "sha256:...",
    "effective_state_hash": "sha256:...",
    "checks": [],
    "warnings": []
  }
}
```

The correct validity test is `result.validation.valid`, not `result.valid`.

### Staging

```json
{
  "status": "ok",
  "deployment": {
    "deployment_id": "dep_...",
    "state": "staged",
    "package_hash": "sha256:..."
  }
}
```

The deployment ID is `result.deployment.deployment_id`.

### Activation and rollback

```json
{
  "status": "ok",
  "deployment": {
    "deployment_id": "dep_...",
    "state": "active",
    "package": {"package_id": "..."}
  }
}
```

Verify `result.deployment.state == "active"`, then call the native active-package tool and compare
`active_deployment_id` and the effective configuration.

### Deployment list

```json
{
  "status": "ok",
  "deployments": [
    {
      "deployment_id": "dep_...",
      "state": "active",
      "package_id": "...",
      "reason": "...",
      "created_by": "..."
    }
  ]
}
```

### Event cursor

```json
{
  "status": "ok",
  "events": [],
  "next_after": 42
}
```

Use `next_after` as the next `after` input. Do not restart from zero in a busy loop.

### Dial

Input:

```json
{
  "destination": "+212...",
  "recording_consent": false
}
```

The result confirms only that PhoneAgent accepted/started the dial operation. Monitor events/status
for answer, no-answer, failure and completion.

## Read-only baseline recipe

Call directly, in order:

1. `mcp__phoneagent__phone_agent_status`
2. `mcp__phoneagent__phone_agent_capabilities`
3. `mcp__phoneagent__phone_agent_identity`
4. `mcp__phoneagent__phone_agent_control_schema`
5. `mcp__phoneagent__phone_agent_get_active_package`
6. `mcp__phoneagent__phone_agent_list_deployments`

Do not use terminal commands to reproduce information already returned by these tools.

## Package customization recipe

1. Call the native active-package tool.
2. Clone the complete `package` object in agent context.
3. Change only requested declarative fields.
4. Call native validation with `{"package": cloned_package}`.
5. Read `validation.valid`, checks and warnings.
6. If valid and the user authorized deployment, call native staging with package, reason and
   `created_by: "hermes-agent"`.
7. Read `deployment.deployment_id`.
8. Call native activation with that ID.
9. Read back the active package and verify channel/task/package/deployment.

Do not write a script to orchestrate these calls. Native sequential tool use is the orchestration.

## Channel-switch recipe

Channel is a persistent AgentPackage runtime parameter.

1. Read status and active package natively.
2. Record the previous active deployment ID and previous channel.
3. Ask/derive whether the requested switch is persistent or only for the next call.
4. Clone the package and set `package.runtime.call_channel` to `gsm`, `whatsapp_phone`, or
   `whatsapp`.
5. Validate → stage → activate → read back through native tools.
6. Dial only after explicit authorization.
7. If the switch was temporary, roll back to the recorded previous deployment after the call and
   verify read-back.

Do not claim the channel changed until active-package read-back confirms it.

## Calling recipe

Before dialing:

- verify `call_state == IDLE`;
- verify active channel/package/task;
- verify the destination is explicitly authorized;
- pass recording consent truthfully;
- do not activate another package during the call.

Use native `phone_agent_dial`, then monitor native event/status tools. A DIALING event is not proof
of answer. Report answered, no-answer or failure only from authoritative events/state.

For a human-gated call use native request-dial, wait for exact Studio approval, then native
execute-approved-dial.

## Stop conditions

Stop rather than creating a fallback wrapper when:

- the native tool is missing after reload;
- the response violates its documented envelope;
- validation is invalid;
- stage/activation returns stale state;
- a call is already active;
- a real destination was not explicitly authorized;
- the requested action would alter protected media/framework code.

Report the exact native tool, structured error and last verified PhoneAgent state.
