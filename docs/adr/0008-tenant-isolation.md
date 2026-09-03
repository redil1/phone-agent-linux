# ADR-0008 — Tenant identity and isolation are mandatory boundaries

Status: Accepted — implementation in transition

Date: 2026-09-02

## Context

A universal commercial platform will host unrelated organizations, agents, callers, credentials,
knowledge, memory, recordings, tools, workflows, and audit records. Application filters added later
cannot reliably repair unscoped identifiers or shared caches.

Current conformance: the current installation is a qualified single-operator appliance with private
files and caller-bound controls. It is not certified for shared multi-tenant production; tenant IDs,
row-level enforcement, per-tenant keys, quotas, and isolation tests remain M16 work.

## Decision

Tenant is a required security principal and namespace in every persistent identifier, authorization
decision, cache key, index, event, job, metric dimension, secret reference, tool connection, and
deployment. Server-side identity derives tenant membership; clients and prompts cannot assert it.
Encryption keys, retention, quotas, residency, exports, deletion, and audit are tenant-scoped.
Higher-risk or regulated tiers may use database, process, network, or deployment isolation profiles.

## Invariants

- Cross-tenant data, memory, tool, secret, cache, workflow, and control-plane leakage is zero.
- Every query and mutation enforces tenant scope below the model/prompt layer.
- Background tasks retain the initiating tenant/principal and least privilege.
- Telemetry and support access redact identifiers and are auditable.
- Backup, restore, export, deletion, and key rotation preserve tenant boundaries.

## Alternatives considered

- One installation per customer forever: rejected as the only architecture, though retained as a
  strong isolation deployment profile.
- Add tenant filters in Studio/API handlers: rejected because workers and storage can bypass them.
- Trust package-supplied tenant IDs: rejected because package content is not authentication.

## Consequences

Schemas and APIs carry more context, and cache/index design becomes stricter. The platform can then
offer credible enterprise isolation, regional deployment, per-tenant cost controls, and audit.

## Migration and rollback

M16-01 through M16-12 introduce tenant models, RBAC/ABAC, row-level enforcement, keys, quotas,
residency, and isolation tests. Existing data migrates into an explicit default tenant with a
verified manifest. Rollback is permitted only while preserving that tenant label and isolation.

## Verification

`docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md` M16-01, M16-04, M16-05, and M16-12 define proof. The
launch gate requires zero cross-tenant leakage under automated adversarial tests.

## Supersession

Changes to isolation technology require a new threat model and migration evidence if they alter a
boundary. No superseding ADR may make tenant identity optional in shared production.
