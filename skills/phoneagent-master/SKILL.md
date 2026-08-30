---
name: phoneagent-master
description: Understand, configure, extend, diagnose, test, deploy, or operate the PhoneAgent framework end to end, including Android/GSM, WhatsApp voice, OpenAI Realtime S2S, identity, tasks, skills, memory, tools/MCP, web research, OpenWA, CRM/ERP, Studio, packaging, and external-agent control. Use for work on this PhoneAgent repository or installation; do not use for unrelated telephony systems.
---

# PhoneAgent Master

Use this skill to work on PhoneAgent as one protected execution platform rather than a collection of
unrelated scripts. Preserve call quality and the working media paths while changing behavior through
validated configuration whenever possible.

## Locate the system

The canonical development checkout is normally:

```text
/Users/aziz/Desktop/PhoneAgent/phone_agent_gateway
```

Do not assume that path on another Mac. Find the root by locating `pyproject.toml`,
`ai_bridge/phone_voice_agent.py`, `android_service_apk/`, and
`release/frozen-whatsapp.sha256`. Use absolute paths in commands and reports.

The installed native runtime normally lives under:

```text
~/.local/share/phone-agent/runtime
```

Persistent configuration lives under `~/.config/phone-agent/`; runtime data, memory, recordings,
qualification reports and backups live under `~/.local/share/phone-agent/`. Never print or copy
those directories wholesale: they may contain credentials and customer data.

## Non-negotiable boundaries

1. **Do not change GSM or WhatsApp media code for configuration work.** Identity, tasks, skills,
   memory, tools, business systems and external orchestration sit above media.
2. **The direct WhatsApp implementation is frozen.** Before and after relevant work run:

   ```bash
   uv run python tools/verify_frozen_whatsapp.py
   ```

   Never update `release/frozen-whatsapp.sha256` during ordinary work. A manifest change requires a
   separate explicit requalification project.
3. **Do not infer permission to place a real call.** A build, test or diagnosis request permits
   loopback and read-only checks, not contacting a person. Use hardware-marked or real-number tests
   only when explicitly authorized.
4. **Caller-bound tools remain caller-bound.** The model and external administrator must not choose
   an arbitrary WhatsApp recipient, CRM customer or support-ticket owner. PhoneAgent injects the
   authenticated current-call number.
5. **Never expose secrets.** Public APIs return masks. Preserve masked values through the stores;
   use secret references rather than placing credentials in prompts, logs, packages or examples.
6. **Do not activate global behavior during a live call.** Identity and AgentPackage activation must
   fail while a call owns the runtime. Tool integrations may hot-reload only through their existing
   bounded paths.
7. **Keep the one-call hardware lock, consent, do-not-call, audit and rate policies intact.** An
   external-agent administrator may configure and operate PhoneAgent but may not bypass these
   execution invariants.
8. **Do not treat model narration as proof.** Verify tool results, database state, WhatsApp
   confirmation state, call completion and audio diagnostics from authoritative events/backends.
9. **Hermes must use its native PhoneAgent MCP tools.** When `mcp__phoneagent__...` tools are
   available, call them directly. Do not invoke `mcporter`, create temporary wrapper scripts, call
   raw REST, or launch a second `phone-agent-mcp` process for normal operation. Those are diagnostic
   fallbacks only when native MCP is unavailable and the user explicitly asked for diagnosis.

## Route the task

Read only the references needed for the current request:

- For system structure, process ownership, ports and trust boundaries, read
  [references/architecture.md](references/architecture.md).
- For inbound/outbound call behavior, audio, turn-taking, Realtime, interruption and hang-up, read
  [references/call-lifecycle.md](references/call-lifecycle.md).
- For persona, task, skill, memory, knowledge, call context and runtime customization, read
  [references/agent-configuration.md](references/agent-configuration.md).
- For Codex/Hermes control, AgentPackage, MCP, REST, deployment and rollback, read
  [references/control-plane.md](references/control-plane.md).
- When operating PhoneAgent from Hermes, read
  [references/hermes-native-mcp.md](references/hermes-native-mcp.md) before the first tool call.
- For generic tools, OpenWA, web research, Frappe CRM/ERP/Helpdesk and campaigns, read
  [references/tools-and-business.md](references/tools-and-business.md).
- For installation, qualification, testing, logs, backup, release and troubleshooting, read
  [references/operations-and-quality.md](references/operations-and-quality.md).
- For finding implementation code quickly, read
  [references/source-map.md](references/source-map.md).

## Choose the correct customization layer

Use the narrowest authoritative layer that expresses the requested outcome:

| Need | Correct layer |
| --- | --- |
| Name, role, mission, values, disclosure, speaking identity | Identity profile |
| Objective, stages, slots, strategy, knowledge, objections, stop conditions | Task contract |
| Reusable specialist procedure loaded only when relevant | Trusted skill |
| Durable approved facts or operator directives | Mutable memory block |
| One temporary call/job instruction | Runtime `system_prompt` in a staged AgentPackage |
| Model, voice, speed, VAD, language, channel, auto-answer | Runtime control profile |
| External action or data source | Tool/MCP connection plus task allowlist |
| WhatsApp chat interaction | OpenWA configuration and current-caller tools |
| Live internet evidence | Web Research configuration |
| Leads, support, orders, invoices, campaigns | Frappe business configuration |
| Coordinated change across several layers | Versioned AgentPackage deployment |

Do not put product prices in persona, put personality in a task, encode permissions inside a skill,
or put a customer-specific fact in global memory. A skill can explain how to use a tool but cannot
grant the tool.

## Standard work sequence

1. Identify whether the request is explanation, diagnosis, configuration, implementation,
   deployment, or live operation.
2. Read the relevant reference and inspect the exact source/config currently active. Existing docs
   are guides; Python/Pydantic schemas and the installed runtime are authoritative.
3. Check `git status --short`. Treat all existing changes as user-owned and avoid unrelated files.
4. Run the frozen WhatsApp verifier before code work near call transports, Android, Realtime or
   packaging.
5. Capture a read-only baseline: Studio status, selected channel/task, inbound state, relevant
   integration health, and device state when GSM is in scope.
   From Hermes, obtain this baseline with native `mcp__phoneagent__...` tools—not a shell wrapper.
6. Prefer an AgentPackage or existing store/API change over source modification. Modify code only
   when the framework lacks the necessary declarative capability or contains a defect.
7. Trace the complete producer → transport → consumer → persisted result path before changing a
   cross-component behavior.
8. Add regression tests reproducing the real failure, including both Realtime transports when tool
   execution or call behavior is shared.
9. Run focused tests, then full lint/compile/tests and the frozen verifier.
10. Install with `tools/install_macos.sh` when native Python/UI code changed. Rebuild/restart the
    Compose stack only when its files or custom Frappe app changed.
11. Verify the installed runtime, not only the checkout. For external control, exercise
    clone → validate → stage → activate → read-back; test rollback when deployment logic changed.
12. Report what was proven automatically, what was proven against live local services, and what
    still needs an explicitly authorized real call.

## Review and diagnosis principles

- Follow IDs end to end: deployment ID, call ID, Realtime function-call ID, WhatsApp message ID,
  Frappe document ID and audit event.
- Distinguish generated, played, interrupted and completed speech. Transcript presence is not proof
  the caller heard the line.
- Distinguish WhatsApp accepted, confirmed in chat, device-delivered and read.
- Distinguish a tool request, verified result, AI narration and backend persistence.
- Distinguish side-channel transcription from the Realtime model's direct audio understanding.
- Distinguish current caller context from a destination the model invented.
- Treat latency by phase: preconnect, first audio, tool execution, evidence retrieval and phone
  playout. Do not optimize by weakening the authoritative turn or playback boundaries.
- If an observed failure contradicts a passing evaluator score, trust the observed event chain and
  add a regression. Evaluation scores are evidence, not reality.

## Completion gates

For ordinary configuration, require schema validation and exact read-back. For code changes, require
focused tests and the full suite. For media-adjacent work, require the frozen verifier and device
qualification. For a production release, require installation health, service health, backup,
rollback evidence and an explicitly authorized physical call matrix.

Never claim universal or future-proof perfection. State production readiness within the qualified
Mac, Android profile, configured providers and tested integrations.
