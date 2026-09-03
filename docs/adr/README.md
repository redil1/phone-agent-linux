# PhoneAgent architecture decision records

Architecture decision records (ADRs) define durable platform constraints. They do not override the
execution plan in `docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md`, grant authority for external
actions, or turn planned behavior into a production claim.

## Status model

- **Proposed:** under review and not binding.
- **Accepted — implementation in transition:** binding target with named, testable legacy drift.
- **Accepted:** binding and verified in the current supported release.
- **Superseded:** replaced by a linked later ADR; retained for history.
- **Rejected:** considered but never adopted.

An accepted ADR changes only through a new superseding ADR. Implementation work that contradicts an
accepted record must stop or propose supersession; editing history to hide the contradiction is not
allowed.

## Initial decision set

| ADR | Status | Decision |
| --- | --- | --- |
| [0001](0001-cascade-only-voice-runtime.md) | Accepted — implementation in transition | One Cascade voice runtime |
| [0002](0002-agent-package-contract.md) | Accepted — implementation in transition | Immutable universal Agent Package |
| [0003](0003-authoritative-state-ownership.md) | Accepted — implementation in transition | Explicit desired/compiled/active/observed state ownership |
| [0004](0004-deterministic-tool-policy.md) | Accepted — implementation in transition | Model proposes; deterministic policy authorizes and verifies |
| [0005](0005-layered-memory-governance.md) | Accepted — implementation in transition | Scoped, consent-aware layered memory |
| [0006](0006-durable-workflows-off-speech-path.md) | Accepted — implementation in transition | Durable workflows stay off the speech critical path |
| [0007](0007-capability-based-provider-routing.md) | Accepted — implementation in transition | Capability-based STT/LLM/TTS routing within Cascade |
| [0008](0008-tenant-isolation.md) | Accepted — implementation in transition | Tenant identity and isolation are mandatory boundaries |
| [0009](0009-transactional-deployment-and-rollback.md) | Accepted — implementation in transition | Transactional activation and verified rollback |

Use [template.md](template.md) for future decisions. Verification tests live in
`tests/test_architecture_decisions.py`.
