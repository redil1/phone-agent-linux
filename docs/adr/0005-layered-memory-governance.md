# ADR-0005 — Scoped, consent-aware layered memory

Status: Accepted — implementation in transition

Date: 2026-09-02

## Context

Natural long calls need continuity, while enterprise use demands deletion, correction, retention,
tenant isolation, and protection against a model turning uncertain speech into durable fact.

Current conformance: PhoneAgent has identity/self blocks, caller memory, working state, reviewed
mutation paths, and tests. It lacks the complete typed schemas, consent/legal-basis controls,
retrieval isolation, lifecycle enforcement, and scale architecture required by M11.

## Decision

Memory is layered: immutable package/identity context; call-local working and task state; governed
caller memory; tenant/organization knowledge; and authoritative external business records. The model
may propose memory candidates only from authoritative final turns or verified tools. Deterministic
policy validates scope, provenance, confidence, consent/legal basis, sensitivity, retention, and
write authority before persistence. Retrieval is tenant-, agent-, caller-, purpose-, and task-bound.

## Invariants

- Provisional, superseded, echoed, or unclear speech cannot become durable memory.
- Call summaries are derived artifacts, never substitutes for authoritative transcript/evidence.
- Callers and operators can inspect, correct, and delete governed memory.
- Sensitive values are minimized, encrypted, redacted from logs, and never embedded in packages.
- External systems remain authoritative for their business records.

## Alternatives considered

- Put the full transcript in every prompt forever: rejected for latency, privacy, and noise.
- Let the model directly edit a memory document: rejected for hallucination and scope leakage.
- Disable memory globally: rejected because useful continuity and workflows require governed state.

## Consequences

Memory writes become explicit events with provenance and retention cost. Context assembly can remain
bounded through summaries and retrieval without losing correction history or auditability.

## Migration and rollback

M11-01 through M11-12 version schemas, consent, retention, deletion, retrieval, and injection
defence. Existing caller memory is classified and migrated conservatively; unclassifiable entries
are quarantined. Rollback restores the prior schema reader and never resurrects deleted data.

## Verification

`docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md` M11-03, M11-05, M11-09, and M11-11 define core proof.
Current tests include `tests/production/test_identity_kernel.py` and memory-related agent-policy and
control-plane regressions.

## Supersession

A later memory design must preserve final-turn authority, provenance, isolation, correction,
deletion, and non-resurrection. A vector database choice does not itself supersede this ADR.
