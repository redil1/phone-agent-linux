# ADR-0004 — Deterministic tool policy and verified effects

Status: Accepted — implementation in transition

Date: 2026-09-02

## Context

A universal agent needs tools, but model fluency is not authorization and a generated success claim
is not evidence. Tool schemas and returned text may also contain malicious or irrelevant content.

Current conformance: permission gates, tool control, approval records, caller-bound catalogs, and
result grounding exist. Visibility, tenant policy, health, risk, idempotency, and evidence are not
yet unified into one capability contract and policy decision point.

## Decision

The model may propose a typed capability request. Deterministic code decides visibility,
authorization, approval, argument constraints, execution, retry/idempotency, result validation,
audit, and caller-safe rendering. A tool is visible only when connected, healthy, schema-valid,
tenant-scoped, task-authorized, and allowed for the principal and current context. Tool output is
untrusted evidence, not instructions. Consequential success is reported only from verified backend
evidence.

## Invariants

- A request boolean or prompt text cannot grant operator authority.
- Deny is the default for unknown tools, principals, tenants, actions, resources, or risk.
- High-risk or irreversible actions require the configured human approval and fresh context.
- Tool execution is bounded, cancellable, observable, and protected against duplicate effects.
- Raw control payloads and untrusted tool instructions never reach speech.

## Alternatives considered

- Give the model every connected tool: rejected due prompt injection and excess authority.
- Encode permissions only in the system prompt: rejected because prompts are not enforcement.
- Report success from plausible model output: rejected because it creates unsupported claims.

## Consequences

Capability manifests and policy evaluation add latency and engineering work, so policy decisions
must be precompiled and measured. The reward is predictable security, exact audit, and portable
tools across personas and tasks.

## Migration and rollback

M9-01 through M9-14 introduce universal manifests, PDP/PEP enforcement, approval, sandboxing, and
conformance. Existing tools enter deny-by-default until mapped. Rollback disables a capability or
restores the previous policy bundle without modifying conversation history.

## Verification

`docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md` M9-02, M9-05, M9-06, and M9-11 are primary gates.
Current evidence includes `tests/production/test_tool_control.py`,
`tests/test_tool_argument_grounding.py`, and `tests/test_realtime_tool_catalog.py`.

## Supersession

Any replacement must retain independent enforcement, tenant scope, verified effects, audit, and
human approval semantics. Model reliability alone cannot justify supersession.
