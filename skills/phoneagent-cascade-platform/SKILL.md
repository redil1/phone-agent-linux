---
name: phoneagent-cascade-platform
description: Execute the PhoneAgent transformation into a Cascade-only universal Agent Operating System using the versioned end-to-end backlog. Use when planning, implementing, reviewing, testing, migrating, or continuing this platform program; do not use for unrelated applications or ordinary one-off PhoneAgent operations.
---

# PhoneAgent Cascade Platform

Advance the universal platform as one evidence-driven program. The canonical plan is
[docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md](../../docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md).
Read its Plan control, status notation, product invariants, definition of done, current milestone,
and dependency path before changing the project. Read later milestone details only when designing
an interface that must remain compatible with them or when that milestone becomes active.

## Goal

Deliver one production voice pipeline:

```text
audio → authoritative turn → streaming STT → universal agent runtime/LLM
      → validated response/action → streaming TTS → verified phone playout
```

The finished platform must configure any lawful product, service, AI identity, persona, language,
task, knowledge base, tool set, workflow, memory policy, and deployment profile through a versioned
Agent Package without domain-specific runtime forks.

## Non-negotiable decisions

1. Remove speech-to-speech/Realtime audio-model pipelines completely. Do not retain a hidden,
   deprecated, fallback, experimental, or compatibility S2S execution path.
2. Keep Pipecat as the sole real-time Cascade voice framework unless the user explicitly approves a
   future architecture change backed by benchmark and migration evidence.
3. Preserve and continuously qualify the working Android/GSM capture, injection, authenticated
   transport, credit/ACK, generation flush, telephony routing, and audioserver recovery path.
4. Do not put domain wording or business facts in shared runtime code. OXzoon/IPTV becomes one
   Agent Package and regression suite.
5. Let the LLM choose natural language and flexible conversational progress. Deterministic code
   owns turn authority, facts, typed state, permissions, tools, actions, delivery, and audit.
6. Use one live conversational authority. Specialist agents and durable workflows stay outside the
   speech-critical path.
7. Treat the backlog as desired scope, not blanket authorization for real calls, external messages,
   purchases, destructive operations, production deployment, or access to new systems.

## Execution protocol

1. Inspect the active goal, repository status, current runtime, and backlog. Preserve user-owned
   changes and identify the first unblocked item on the critical path.
2. If an earlier item is partially implemented, finish and verify it before starting a later item.
   Parallel work is allowed only when dependencies and shared files do not conflict.
3. For a milestone, first record baseline behavior and add a regression that fails for the missing
   behavior. Do not delete an old path until replacement coverage proves required shared behavior.
4. Implement the smallest coherent vertical slice through schema, runtime, API/UI where applicable,
   telemetry, tests, migration, docs, deployment, and rollback.
5. Use open source only after inspecting the current official repository, maintenance, licence,
   security posture, measured latency, failure semantics, integration cost, and exit strategy.
   Prefer adapters around dependencies so Agent Packages remain portable.
6. Run focused checks during development. Before checking an item, satisfy its definition of done
   and the relevant repository-wide quality gates.
7. Update the checkbox and add `Evidence:` or a linked report in the same commit. Never mark work
   complete because code was drafted, a mocked happy path passed, or a service merely started.
8. At each milestone boundary, verify upgrade, active worker read-back, rollback, observability,
   security, performance, and—when explicitly authorized—hardware behavior.
9. Continue with the next unblocked critical-path item while the active goal and user authorization
   permit meaningful work. Do not stop merely because a subtask is difficult or the plan is long.
10. If new authority or a material product decision is genuinely required, document the exact
    blocker and safe alternatives. Do not silently choose a scope-expanding architecture.

## Backlog maintenance

- Keep stable IDs. Add newly discovered work under the milestone that owns its acceptance outcome.
- Do not delete unfinished requirements to improve apparent progress. Supersede them with rationale
  and a replacement ID.
- Split an item when it cannot be completed and evidenced in one coherent change.
- Record decisions that affect several milestones as ADRs and link them from the relevant items.
- Update target thresholds only from measured evidence and document whether they became stricter or
  looser and why.
- The plan is complete only when all milestone exit gates and the final completion condition pass.

## Durable continuation

Use [reports/CASCADE_EXECUTION_STATE.md](../../reports/CASCADE_EXECUTION_STATE.md) as the durable
checkpoint for this program. If it does not exist, create it before implementation. At the start of
each work cycle, reconcile it with the active goal and canonical backlog; the backlog owns scope and
acceptance, while the checkpoint owns the exact current item and next action.

Update the checkpoint after material evidence, a changed decision, a newly discovered risk, or a
changed blocker, and before yielding after partial work. Preserve at least the active backlog ID,
last verified evidence, unresolved risks, and exact next unblocked action. A paused controller,
context compaction, turn boundary, process restart, or difficult subtask does not erase the program
objective. Resume from the checkpoint when the user or controller resumes execution.

Keep the goal unfinished until every backlog milestone exit gate and the final completion condition
pass. This continuation contract does not override user pauses, required authority, safety limits,
host shutdown, unavailable external systems, or material product decisions that only the user can
make; record those conditions precisely instead of claiming continuous execution.

## Verification hierarchy

Use the strongest available evidence:

1. Authoritative device/backend state and phone playout acknowledgement.
2. End-to-end integration and hardware qualification in an explicitly authorized environment.
3. Provider conformance, audio replay, long-duration, load, chaos, and security tests.
4. Unit/property tests and schema validation.
5. Static inspection and documentation.

Passing a lower level cannot contradict a failure observed at a higher level. Convert every real
failure into a permanent regression case.

## Required handoff

Every work cycle reports:

- Backlog IDs completed and currently active.
- Source/config/deployment artifacts changed.
- Tests, benchmarks, security checks, migrations, deployment, and rollback actually proven.
- Live runtime/package/model/APK hashes when deployment is in scope.
- Known risks, blocked items, and the exact next unblocked backlog item.

Never promise perfect stochastic conversation or a monetary valuation. Build a platform whose
quality, safety, universality, latency, and reliability are demonstrated by evidence.
