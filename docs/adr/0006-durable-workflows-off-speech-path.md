# ADR-0006 — Durable workflows stay off the speech critical path

Status: Accepted — implementation in transition

Date: 2026-09-02

## Context

Bookings, follow-ups, research, approvals, transfers, and payments may outlive a phone turn or
process. Running them as hidden prompt scripts makes retries unsafe; putting multi-agent planning in
the speech loop raises latency and splits conversational authority.

Current conformance: task state and bounded tool calls exist, but the current task engine is not a
universal durable workflow runtime with versioned checkpoints, timers, compensation, and replay.

## Decision

One agent runtime owns the live conversation. Durable work runs as typed, versioned workflows with
explicit states, inputs, deadlines, idempotency keys, retries, compensation, approval checkpoints,
human assignment, audit events, and terminal evidence. Background specialists are tools/workers
that return bounded evidence; they never speak directly or mutate live conversation state. The live
agent may acknowledge, continue, wait, or resume based on workflow events without blocking audio.

## Invariants

- No second conversational authority competes for a call.
- Workflow retries cannot duplicate external effects.
- Long work survives process restart and call termination.
- A workflow completion claim requires its terminal authoritative evidence.
- Workflow versions and package compatibility are explicit and replayable.

## Alternatives considered

- Keep workflows as prompt instructions: rejected because state and retries are unverifiable.
- Run multiple live speaking agents: rejected due latency, context conflict, and unclear authority.
- Block the phone turn until work completes: rejected because carrier conversations require bounded
  response timing and honest progress updates.

## Consequences

A durable orchestrator and event model are required. Speech stays fast and human while complex work
becomes recoverable, observable, testable, and suitable for enterprise operations.

## Migration and rollback

M12-01 through M12-12 introduce schemas, idempotency, timers, compensation, human work, and replay.
Existing task transitions migrate to versioned definitions incrementally. Rollback stops new starts,
lets compatible in-flight versions finish or compensates them, and preserves their audit trail.

## Verification

`docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md` M5-13 and M12-01 through M12-12 are controlling gates.
Deterministic workflow simulation and crash/retry tests must prove no duplicate side effects.

## Supersession

A new orchestration engine may supersede implementation technology only if workflow semantics,
replay, compensation, evidence, and the single-conversation-authority invariant remain intact.
