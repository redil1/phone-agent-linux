# ADR-0003 — Explicit authoritative state ownership

Status: Accepted — implementation in transition

Date: 2026-09-02

## Context

Past WebUI changes appeared saved while a warm worker continued with different providers or models.
Environment variables, stored settings, live call state, memory, and external business state have
different lifecycles and cannot be merged into one ambiguous dictionary.

Current conformance: desired and warm-worker read-back exists, and deployments use hashes, but
environment/store precedence and provider-specific fields can still disagree. The full source map
and transactional state model remain M2 work.

## Decision

State is divided by owner and lifecycle: the control plane owns desired and staged configuration;
the compiler owns immutable compiled artifacts; the deployment coordinator owns the active digest;
each worker owns observed effective state and health; the call session owns ephemeral turn/task
state; governed memory stores own durable learned state; authoritative business systems own orders,
appointments, payments, and other external facts. Studio is a view/client, never an authority.

## Invariants

- Every effective field reports value, origin, revision/digest, and activation boundary.
- Desired, staged, active, and worker-observed state are separately readable and drift-detected.
- External completion is true only when its authoritative backend verifies it.
- Active-call state is isolated from next-call configuration changes.
- Writes use schema validation, compare-and-swap, audit, and atomic persistence.

## Alternatives considered

- Last writer wins across environment and files: rejected because precedence is invisible.
- Treat worker memory as authority: rejected because restarts and multiple workers lose coherence.
- Let Studio cache effective values: rejected because browser state is stale and untrusted.

## Consequences

More explicit schemas and read-back endpoints are required, but configuration changes become
explainable and safe. Operators can distinguish saved intent, activation, worker drift, and call
binding instead of debugging them as one state.

## Migration and rollback

M2-01 defines the unified schema; M2-02 assigns exact precedence and origins; M2-04 makes activation
transactional; M2-05 adds hash-based read-back. Legacy values migrate once with an audit record.
Rollback restores the prior active digest while preserving failed desired/staged evidence.

## Verification

`docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md` M2-01 through M2-10 and the Milestone 2 drift gate are
authoritative. Current deployment-state tests live in `tests/production/test_control_plane.py`; the
baseline desired/worker comparison is recorded under `reports/baselines/`.

## Supersession

Changing an owner or lifecycle requires a new ADR with concurrency, migration, audit, and rollback
proof. A storage technology change that preserves ownership does not supersede this record.
