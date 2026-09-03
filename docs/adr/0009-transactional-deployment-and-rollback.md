# ADR-0009 — Transactional activation and verified rollback

Status: Accepted — implementation in transition

Date: 2026-09-02

## Context

Voice deployments combine packages, compiled prompts/state, provider models, warm workers, protocol
compatibility, APKs, and persisted data. Partial activation can produce a healthy UI while calls use
stale behavior or no audio.

Current conformance: `ControlPlaneStore` supports staged/activating/active/superseded/failed records,
state hashes, stale-base rejection, call-boundary activation, and package rollback. It does not yet
provide complete compile/eval/prewarm health gates, worker digest consensus, signed release
evidence, or APK/schema migration orchestration.

## Decision

Deployment is an immutable transaction: stage package and dependencies → validate and compile → run
policy/security/evals → prewarm providers → health-check media and worker → compare-and-swap the
active digest at a safe call boundary → read back worker digest and health → commit evidence. Active
calls keep their starting digest. Any failed step leaves or restores the prior active release.
Rollback is a first-class, regularly tested deployment producing a new audited activation record.

## Invariants

- No in-place mutation of an active package or compiled artifact.
- A failed activation cannot evict the last healthy warm worker.
- Desired, active, and worker-observed digests must converge before success is reported.
- Schema, protocol, APK, package, and image compatibility are checked before activation.
- Rollback target, evidence, and recovery time are known before rollout.

## Alternatives considered

- Restart services after saving settings: rejected because saving is not activation or verification.
- Update workers gradually without call binding: rejected because one call could span behaviors.
- Treat rollback as reinstalling the previous files manually: rejected because it is slow,
  unaudited, and unsafe for migrated state.

## Consequences

Deployments require orchestration and additional capacity for the previous warm version. In return,
operators get atomic behavior, safe canaries, exact provenance, drift detection, and a target rollback
time of at most 60 seconds for Agent Package activation.

## Migration and rollback

M2-04 adds transactional runtime activation; M4-12 binds calls and rollback; M17-06 through M17-09
add canary, migration compatibility, and disaster recovery. The initial qualified GSM baseline is
retained until the Milestone 0 exit rollback test passes.

## Verification

`docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md` M2-04, M2-05, M4-12, M17-07, and M17-08 are
controlling. Current unit evidence is in `tests/production/test_control_plane.py`; release evidence
format is defined by M0-09.

## Supersession

A deployment-system replacement must demonstrate equivalent atomicity, call binding, digest
read-back, migration safety, audit, and rollback SLO before this ADR can be superseded.
