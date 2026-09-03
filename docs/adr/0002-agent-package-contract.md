# ADR-0002 — Immutable universal Agent Package

Status: Accepted — implementation in transition

Date: 2026-09-02

## Context

Personas, tasks, products, tools, policy, memory, knowledge, models, and voice currently span YAML,
environment variables, stores, prompts, and source code. Universal customization cannot depend on
editing Python or on partially copying active configuration.

Current conformance: `ai_bridge/control_plane.py` provides a strict, hashable package and deployment
record, but its schema is incomplete, contains S2S fields, and lacks the full manifests, signing,
compiler artifacts, and migrations required by the target.

## Decision

Every deployable agent is described by a versioned, strict, secret-free `AgentPackageV1`. Packages
are declarative, deterministic to serialize, content-addressed, signed for release, immutable after
activation, and compiled before use. They include identity, language/channel behavior, products or
services, tasks, knowledge, capabilities, workflows, policy, memory, voice, model routing,
evaluations, compatibility, and deployment intent. Secrets appear only as resolvable references.

## Invariants

- Unknown fields and incompatible versions fail closed.
- A package cannot grant itself authority or smuggle raw secrets.
- An active call is bound to one package and compiled-artifact digest.
- Import/export preserves semantics and provenance and is tamper-evident.
- Five unrelated reference agents compile without source changes before universality is claimed.

## Alternatives considered

- Continue environment/YAML composition: rejected because precedence and completeness are opaque.
- Store an arbitrary prompt blob: rejected because capabilities, facts, state, and permissions are
  not typed or independently verifiable.
- Fork code per customer: rejected because it destroys upgradeability and platform economics.

## Consequences

Schema evolution and migrations become product responsibilities. Studio becomes a package editor
and compiler client, not an alternate source of truth. Compilation can produce minimal prompts,
capability plans, retrieval plans, state schemas, policy bundles, and generated evaluations.

## Migration and rollback

M3-12 imports current identity, tasks, tools, memory, integrations, and runtime settings without
losing operator data. M4-12 binds calls to immutable revisions and restores the previous digest on
activation failure. Export remains secret-free so rollback artifacts can be retained safely.

## Verification

`docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md` M3-01 through M3-12 define schema acceptance; M4-01
through M4-11 define compiler proof. Current boundary tests are in
`tests/production/test_control_plane.py`; strict typing covers `ai_bridge/control_plane.py`.

## Supersession

A new major package schema requires a compatibility ADR, deterministic migration, dual-read window
if needed, and rollback evidence. It cannot silently reinterpret a v1 field.
