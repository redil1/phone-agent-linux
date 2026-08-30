# Connect Hermes Agent to PhoneAgent — Complete End-to-End Guide

This guide connects Nous Research Hermes Agent to PhoneAgent on macOS. After setup, Hermes can read,
customize, validate, stage, activate, operate, monitor and roll back PhoneAgent through MCP without
editing PhoneAgent framework or media code.

The connection has two parts:

1. **PhoneAgent MCP server** — gives Hermes 16 structured control tools and 3 resources.
2. **PhoneAgent master skill** — teaches Hermes the complete framework architecture, configuration
   layers, protected boundaries, testing requirements and operating workflow.

Using MCP without the skill gives Hermes functions but less architectural understanding. Using the
skill without MCP gives Hermes knowledge but no direct PhoneAgent control. Install both.

## 1. Architecture

```text
You
 │
 ▼
Hermes Agent
 │  local MCP over stdio
 ▼
phone-agent-mcp
 │  private loopback bearer authentication
 ▼
PhoneAgent Control Plane · http://127.0.0.1:8090
 │
 ├── AgentPackage configuration and deployment
 ├── Identity · Tasks · Skills · Memory · Voice
 ├── Tools · Web Research · OpenWA · CRM/ERP
 └── Calls · Events · Monitoring · Rollback
      │
      ▼
Protected GSM / WhatsApp execution framework
```

Hermes never receives PhoneAgent's private bearer token. The local MCP process reads it internally.
Hermes cannot modify Android privileged routing, GSM media, frozen WhatsApp media, codecs, PCM
framing, secret-redaction rules, audit integrity or framework source through AgentPackage.

## 2. Requirements

Before connecting Hermes, confirm:

- PhoneAgent is installed and running.
- The full business suite is running when CRM, Helpdesk, OpenWA or Crawl4AI are needed.
- Hermes Agent is installed and authenticated.
- Hermes MCP extras are installed. Standard Hermes installations already include them.
- Both applications run under the same macOS user account.

PhoneAgent's installed MCP executable should be:

```text
~/.local/share/phone-agent/runtime/.venv/bin/phone-agent-mcp
```

Hermes' default configuration is normally:

```text
~/.hermes/config.yaml
```

## 3. Verify PhoneAgent first

Open Terminal and run:

```bash
curl -fsS http://127.0.0.1:8090/api/status
```

Expected important values:

```json
{
  "status": "ok",
  "call_state": "IDLE",
  "inbound_receptionist": {
    "state": "listening"
  }
}
```

Confirm the MCP executable exists:

```bash
test -x "$HOME/.local/share/phone-agent/runtime/.venv/bin/phone-agent-mcp" \
  && echo "PhoneAgent MCP ready"
```

If GSM will be used, confirm Android:

```bash
adb devices -l
```

The qualified phone must appear as `device`, not `unauthorized`.

For a complete local health check:

```bash
cd /absolute/path/to/phone_agent_gateway
./tools/business_suite_status.sh
uv run phone-agent-qualify --ensure-forwards
```

Do not continue to real-call testing when PhoneAgent is unhealthy.

## 4. Verify and optionally update Hermes

Check the installed version:

```bash
hermes --version
hermes update --check
```

To update safely with a full pre-update backup:

```bash
hermes update --backup
```

Updating is optional when the installed version already supports `hermes mcp add`, `test`,
`configure` and `/reload-mcp`.

Confirm MCP management exists:

```bash
hermes mcp --help
```

If Hermes was installed without MCP extras, install them inside the Hermes checkout:

```bash
cd "$HOME/.hermes/hermes-agent"
uv pip install -e ".[mcp]"
```

## 5. Install PhoneAgent knowledge into Hermes

Copy the complete reusable skill into Hermes as regular files. Avoid a symlink because Hermes skill
security intentionally treats links cautiously.

```bash
mkdir -p "$HOME/.hermes/skills/phoneagent-master"
ditto \
  "/absolute/path/to/phone_agent_gateway/skills/phoneagent-master" \
  "$HOME/.hermes/skills/phoneagent-master"
```

For this Mac's usual checkout, the command is:

```bash
mkdir -p "$HOME/.hermes/skills/phoneagent-master"
ditto \
  "/Users/aziz/Desktop/PhoneAgent/phone_agent_gateway/skills/phoneagent-master" \
  "$HOME/.hermes/skills/phoneagent-master"
```

Verify the skill:

```bash
test -f "$HOME/.hermes/skills/phoneagent-master/SKILL.md" \
  && echo "PhoneAgent master skill installed"
hermes skills list | grep -i phoneagent
```

Repeat the `ditto` command after a PhoneAgent update that changes the master skill.

### Make PhoneAgent governance mandatory

Hermes does not currently expose a universal normal-chat `skills.always_load` switch that reliably
preloads a skill across CLI, TUI and gateway sessions. Use its supported persistent context layers:

1. Add a mandatory PhoneAgent rule to `~/.hermes/SOUL.md` requiring `phoneagent-master` before every
   PhoneAgent request/tool call.
2. Set the same rule as `agent.system_prompt` for defense in depth.

Example persistent overlay:

```bash
hermes config set --force agent.system_prompt \
  "MANDATORY PHONEAGENT GOVERNANCE: Before every PhoneAgent request or mcp__phoneagent__ tool call, load phoneagent-master and follow it. Use native PhoneAgent MCP directly; never use mcporter or wrapper scripts while native tools are healthy. Never place a real call without explicit authorization."
```

Confirm it:

```bash
hermes config get agent.system_prompt --json
```

`--safe-mode` and `--ignore-rules` intentionally disable custom context and remain emergency
troubleshooting exceptions. Do not use them for normal PhoneAgent operation.

## 6. Add the PhoneAgent MCP server to Hermes

Run:

```bash
hermes mcp add phoneagent \
  --command "$HOME/.local/share/phone-agent/runtime/.venv/bin/phone-agent-mcp" \
  --connect-timeout 30
```

This is a local stdio server. Do not configure a URL, token, API key or environment secret.

Hermes performs discovery while adding the server. If it asks which tools to expose, select the
PhoneAgent tools required for your operating mode. For complete administrator control, select all 16.

## 7. Configure complete PhoneAgent access

Run the interactive configurator:

```bash
hermes mcp configure phoneagent
```

Recommended values for this locally owned server:

| Setting | Value | Reason |
| --- | --- | --- |
| Enabled | Yes | Connect at Hermes startup/reload |
| Transport | stdio | PhoneAgent MCP is a local process |
| Connect timeout | 30 seconds | Enough for local startup/discovery |
| Tool timeout | 120 seconds | Covers deployment operations without unbounded waits |
| Trust | full | Only for this exact local PhoneAgent executable |
| Parallel tool calls | false | Deployment/call ownership must remain serialized |
| Resources | true | Allows AgentPackage schema/state resources |
| Prompts | false | PhoneAgent does not expose MCP prompts |

Do not mark an unknown or remote MCP server `full`. This recommendation applies only to the
PhoneAgent executable installed under your own user account.

### Full tool allowlist

Hermes filters use the original MCP names below, not the `mcp__phoneagent__` names shown to the model.

```yaml
tools:
  include:
    - phone_agent_status
    - phone_agent_capabilities
    - phone_agent_identity
    - phone_agent_request_dial
    - phone_agent_execute_approved_dial
    - phone_agent_control_schema
    - phone_agent_get_active_package
    - phone_agent_validate_package
    - phone_agent_stage_package
    - phone_agent_list_deployments
    - phone_agent_activate_deployment
    - phone_agent_rollback_deployment
    - phone_agent_recent_events
    - phone_agent_list_tasks
    - phone_agent_dial
    - phone_agent_hangup
  resources: true
  prompts: false
```

For initial read-only qualification, temporarily include only:

```yaml
tools:
  include:
    - phone_agent_status
    - phone_agent_capabilities
    - phone_agent_identity
    - phone_agent_control_schema
    - phone_agent_get_active_package
    - phone_agent_validate_package
    - phone_agent_list_deployments
    - phone_agent_recent_events
    - phone_agent_list_tasks
  resources: true
  prompts: false
```

Add deployment and calling tools after read-only tests pass.

## 8. Manual YAML configuration fallback

Normally, use `hermes mcp add` and `configure`. If manual repair is needed, first back up the config:

```bash
cp -p "$HOME/.hermes/config.yaml" \
  "$HOME/.hermes/config.yaml.before-phoneagent"
```

Then add this under the top-level `mcp_servers` mapping in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  phoneagent:
    command: "/Users/aziz/.local/share/phone-agent/runtime/.venv/bin/phone-agent-mcp"
    args: []
    env: {}
    enabled: true
    timeout: 120
    connect_timeout: 30
    protocol: legacy
    supports_parallel_tool_calls: false
    trust: full
    tools:
      include:
        - phone_agent_status
        - phone_agent_capabilities
        - phone_agent_identity
        - phone_agent_request_dial
        - phone_agent_execute_approved_dial
        - phone_agent_control_schema
        - phone_agent_get_active_package
        - phone_agent_validate_package
        - phone_agent_stage_package
        - phone_agent_list_deployments
        - phone_agent_activate_deployment
        - phone_agent_rollback_deployment
        - phone_agent_recent_events
        - phone_agent_list_tasks
        - phone_agent_dial
        - phone_agent_hangup
      resources: true
      prompts: false
```

PhoneAgent currently supports the established MCP initialize/tools/resources protocol, so
`protocol: legacy` is deterministic. Hermes' default `auto` is also compatible because it tries the
legacy handshake first.

Never paste the PhoneAgent control token into this YAML.

## 9. Test the MCP connection

Run:

```bash
hermes mcp test phoneagent
hermes mcp list
```

Expected result:

- server enabled;
- stdio process starts;
- 16 PhoneAgent tools discovered when the full allowlist is active;
- resources capability discovered;
- no authentication key requested.

If you changed filters after adding the server, start Hermes and run:

```text
/reload-mcp
```

Hermes tool names become:

```text
mcp__phoneagent__phone_agent_status
mcp__phoneagent__phone_agent_get_active_package
mcp__phoneagent__phone_agent_validate_package
```

These native Hermes tools are the required normal operating path. Do not use `mcporter`, a temporary
Python/shell wrapper, raw REST, or a second manually launched MCP process while the native tools are
available. Those are diagnosis-only fallbacks after `hermes mcp test phoneagent` and `/reload-mcp`
fail.

Resource utility wrappers may appear as:

```text
mcp__phoneagent__list_resources
mcp__phoneagent__read_resource
```

## 10. Start Hermes in the PhoneAgent project

```bash
cd /Users/aziz/Desktop/PhoneAgent/phone_agent_gateway
hermes chat
```

Starting in the project gives Hermes the correct working context. Do not use `--safe-mode`, because
safe mode intentionally disables MCP servers and custom skills. Do not use `--yolo` for production
calling.

To preload the master skill explicitly when needed:

```bash
hermes chat --skills phoneagent-master
```

If an existing Hermes session started before MCP/skill installation, start a new session or run:

```text
/reload-mcp
```

## 11. First read-only Hermes test

Give Hermes this prompt:

```text
Use the phoneagent-master skill and PhoneAgent MCP. Read PhoneAgent status, capabilities,
identity, AgentPackage schema and active package. Do not modify, stage, activate, dial or hang up.
Explain the active task, channel, tools, business integrations and immutable boundaries.
```

Hermes should call read-only tools and report:

- Studio/call state;
- selected channel and task;
- active identity and package;
- AgentPackage schema version;
- available integrations;
- protected media/framework boundaries.

If Hermes answers from memory without calling PhoneAgent, tell it explicitly to call the MCP tools
and report their structured results.

## 12. Dry-run a custom agent without changing PhoneAgent

Example prompt:

```text
Use the phoneagent-master skill. Clone the complete active PhoneAgent AgentPackage. Prepare a
bilingual customer-support agent for IPTV installation problems. Preserve every masked credential,
existing integration and protected boundary. Configure identity, task strategy, required inputs,
knowledge, support skills, CRM/Helpdesk tools, voice and inbound auto-answer. Call
phone_agent_validate_package only. Do not stage, activate or place a call. Show the effective diff
and every validation warning.
```

Hermes must clone the active package. It must not construct a partial package from memory because the
schema is strict and masked credentials/integrations must be preserved.

Validation is read-only. It checks:

- identity contract and trusted skills;
- task schema;
- runtime/provider compatibility;
- mutable memory restrictions;
- generic tool configuration;
- OpenWA configuration;
- web-research configuration;
- Frappe business configuration;
- task-to-tool availability.

## 13. Stage and activate a package

After reviewing the dry run, tell Hermes:

```text
Stage the exact validated package with reason "IPTV installation support agent" and created_by
"hermes-phoneagent-admin". Report the deployment ID and package hash. Then activate that deployment,
read the active package back, and verify the active deployment ID and effective state. Do not dial.
```

The lifecycle is:

```text
Read current → Clone → Modify → Validate → Stage → Activate → Read back
```

Activation fails safely when:

- a call is active;
- configuration changed after staging;
- a strict schema is invalid;
- required skills are unavailable;
- identity has a critical contract failure;
- private configuration cannot be written.

If Hermes receives a stale-state error, it must read the active package again, reapply only the
intended changes, validate and create a new stage. It must not reuse or manually edit the failed
deployment file.

## 14. Make a real call through Hermes

Never let a generic test prompt contact an arbitrary person. Supply an authorized test number
explicitly.

Prompt example:

```text
Use the active PhoneAgent package to call my authorized test number +COUNTRYCODE_NUMBER.
Recording consent is false. Monitor PhoneAgent events. During the call, do not change or activate
another package. If I ask to stop, use PhoneAgent hang-up. After completion, report the call outcome,
tool results and backend records separately.
```

Hermes can use one of two dialing workflows:

### Administrator MCP dial

`phone_agent_dial` treats the local PhoneAgent control token as administrator authority. PhoneAgent
still applies destination normalization, rate/cooldown, recording consent, hardware preflight and
one-call locking.

### Explicit one-time approval workflow

For a human-gated call:

1. `phone_agent_request_dial`
2. Approve the exact request in PhoneAgent Studio.
3. `phone_agent_execute_approved_dial`

This is preferable while initially qualifying Hermes.

## 15. Monitor the live call

Hermes should poll `phone_agent_recent_events` using the returned sequence cursor rather than
restarting from zero repeatedly.

Important events include:

- call state/context;
- caller and assistant transcripts;
- playback generated/playing/completed/interrupted;
- tool calls and verified results;
- OpenWA confirmation states;
- AgentPackage activation events;
- AI end-call request and call completion;
- audio-quality diagnostics.

Hermes must distinguish:

- generated speech from speech actually played;
- accepted WhatsApp sends from delivered/read messages;
- model tool arguments from backend persistence;
- transcript content from authenticated caller routing;
- task outcome from a separate CRM/support action.

## 16. End or stop a call

Normally the PhoneAgent calling AI decides naturally when the conversation is complete and invokes
its `end_call` tool.

For administrator intervention, Hermes can call:

```text
phone_agent_hangup
```

After hang-up, verify:

- PhoneAgent returns to `IDLE`;
- inbound receptionist returns to `listening` when enabled;
- the child call process no longer owns the voice lock;
- call outcome and business records were written.

## 17. Verify backend actions

Do not accept “I created it” as database proof.

For support tickets verify:

- ticket ID;
- exact title/description;
- open/resolved status and priority;
- current-caller association;
- creation timestamp and integration owner;
- linked CRM lead/customer when present.

For WhatsApp verify the exact state:

- accepted;
- confirmed in chat;
- device delivered;
- read;
- failed.

For web research inspect sources, publication dates, warnings and search count. For commerce, draft
quotation/order does not mean submitted, paid or delivered.

## 18. Roll back a bad package

Tell Hermes:

```text
List PhoneAgent deployments. Roll back to deployment DEPLOYMENT_ID with reason
"Restore last qualified agent" and created_by "hermes-phoneagent-admin". Read back the active
package and verify it. Do not place a call.
```

Rollback creates a new deployment from the historical package. It preserves history and does not
rewrite an old record.

Use the correct recovery level:

- Agent behavior regression → AgentPackage rollback.
- Native app/runtime release failure → `tools/rollback_macos.sh`.
- CRM/ERP database loss → `tools/restore_business_suite.sh ... --confirm`.

## 19. Example full Hermes job request

```text
Use phoneagent-master and PhoneAgent MCP.

Objective: create an advanced inbound customer-service agent for IPTV installation and buffering
problems.

Requirements:
- English and French;
- warm, concise and technically competent;
- identify the caller's device, application, connection type and exact symptom;
- use live research only when current external information is necessary;
- use CRM and Helpdesk for verified caller context and tickets;
- send requested summaries through WhatsApp to the authenticated current caller only;
- never claim delivery, payment, fulfillment or resolution without backend evidence;
- preserve GSM, WhatsApp media and all protected boundaries.

Workflow:
1. Read schema and active package.
2. Clone and customize the full package.
3. Validate only and show me the diff.
4. Wait for my instruction before staging.
5. After approval, stage, activate and read back.
6. Do not dial until I explicitly provide an authorized test number.
```

## 20. Troubleshooting

### Hermes reports no MCP servers

```bash
hermes mcp list
```

If empty, run the `hermes mcp add phoneagent ...` command again.

### MCP server fails to start

```bash
test -x "$HOME/.local/share/phone-agent/runtime/.venv/bin/phone-agent-mcp"
curl -fsS http://127.0.0.1:8090/api/status
```

Reinstall PhoneAgent if the executable is missing:

```bash
cd /absolute/path/to/phone_agent_gateway
./tools/install_macos.sh
```

### Connection works but tools are missing

Run:

```bash
hermes mcp configure phoneagent
```

Check `tools.include`, then run `/reload-mcp`. Hermes filters use original names such as
`phone_agent_status`, not prefixed names.

### Resources are missing

Set:

```yaml
tools:
  resources: true
```

PhoneAgent has no MCP prompts, so `prompts: false` is expected.

### Hermes cannot see the PhoneAgent skill

```bash
test -f "$HOME/.hermes/skills/phoneagent-master/SKILL.md"
hermes skills list | grep -i phoneagent
```

Start a new Hermes session or explicitly preload it with `--skills phoneagent-master`.

### Hermes is in safe mode

`--safe-mode` disables MCP and custom skills by design. Restart Hermes normally.

### Package validation fails

Do not remove strict fields. Read the active package, clone it and change only required values.
Preserve every masked secret exactly. Resolve missing task tools or trusted skills.

### Activation says configuration changed

Another operator/agent changed PhoneAgent after staging. Read current state and create a new stage.

### Call cannot start

Check PhoneAgent status, ADB, device qualification, active channel, one-call lock, rate/cooldown and
recording-consent input. Do not bypass the policy.

### Remove or temporarily disable the connection

Remove:

```bash
hermes mcp remove phoneagent
```

Or keep the config and set:

```yaml
mcp_servers:
  phoneagent:
    enabled: false
```

Then run `/reload-mcp`.

## 21. Security rules

- Never put `~/.config/phone-agent/control.token` in Hermes configuration or prompts.
- Use `trust: full` only for the exact local PhoneAgent binary you own.
- Keep parallel calls disabled.
- Do not run Hermes with `--yolo` for production calling.
- Use read-only tools first, then deployment, then authorized call testing.
- Never activate a package during a call.
- Never ask Hermes to patch PhoneAgent media/framework code through this workflow.
- Treat transcripts and MCP events as customer data.
- Verify business actions against their authoritative backends.
- Respect consent, do-not-call, calling hours, disclosure and retention obligations.

## 22. End-to-end completion checklist

- [ ] PhoneAgent Studio is healthy and IDLE.
- [ ] Qualified Android device is connected when GSM is required.
- [ ] Business services required by the task are healthy.
- [ ] Hermes MCP support is installed.
- [ ] `phoneagent-master` exists under `~/.hermes/skills/`.
- [ ] `hermes mcp list` shows `phoneagent` enabled.
- [ ] `hermes mcp test phoneagent` succeeds.
- [ ] Required tools and resources are visible after `/reload-mcp`.
- [ ] Hermes completes the read-only status/schema/package test.
- [ ] Package dry-run validation succeeds.
- [ ] Staged deployment activates and reads back exactly.
- [ ] Rollback has been tested before production use.
- [ ] An explicitly authorized real call proves two-way audio.
- [ ] Live tool calls are verified against OpenWA/Frappe/research backends.
- [ ] AI-controlled ending physically closes the call.
- [ ] PhoneAgent returns to inbound listening.

When every relevant item passes, Hermes is connected as PhoneAgent's external administrator and
orchestration agent while PhoneAgent remains the protected call-execution framework.

## Official references

- Hermes MCP guide:
  `https://github.com/NousResearch/hermes-agent/blob/main/website/docs/guides/use-mcp-with-hermes.md`
- Hermes MCP configuration reference:
  `https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/mcp-config-reference.md`
- PhoneAgent control-plane guide: `docs/EXTERNAL_AGENT_CONTROL_PLANE.md`
- PhoneAgent master skill: `skills/phoneagent-master/SKILL.md`
