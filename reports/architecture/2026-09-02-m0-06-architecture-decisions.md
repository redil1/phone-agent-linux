# M0-06 — Architecture decision record baseline

Date: 2026-09-02 UTC

## Outcome

PhoneAgent now has an indexed, governed ADR system and nine accepted target decisions covering the
entire M0-06 scope:

1. one Cascade voice runtime;
2. immutable universal Agent Packages;
3. explicit authoritative state ownership;
4. deterministic tool authorization and verified effects;
5. scoped, consent-aware layered memory;
6. durable workflows outside the speech critical path;
7. capability-based provider routing inside Cascade;
8. mandatory tenant identity and isolation; and
9. transactional deployment and verified rollback.

The index, status model, supersession rule, and future template live in `docs/adr/README.md` and
`docs/adr/template.md`.

## Truthfulness rule

All nine records are `Accepted — implementation in transition`. Each states current conformance and
names remaining drift. This is materially different from claiming the implementation already meets
the target. ADR-0001, for example, names executable ChatGPT/OpenAI Realtime modules and S2S fields as
forbidden transition debt, binds their removal to M1, and explicitly forbids S2S as rollback.

Each record includes context, decision, invariants, alternatives, consequences, migration and
rollback, verification, and supersession. The records link the decision to concrete backlog IDs,
source/tests, and future acceptance gates.

## Verification

- ADR governance regressions: 3 passed.
- Combined focused M0 governance regressions: 10 passed.
- Configured Ruff: pass.
- Strict Pyright boundary gate: 0 diagnostics.
- Full isolated non-device suite: 847 passed, 40 skipped, 4 device tests deselected, 3 known
  deprecation warnings, 52.81 seconds.
- Sensitive-term scan of ADRs found no embedded credential material.

## Record hashes

| Record | SHA-256 |
| --- | --- |
| `0001-cascade-only-voice-runtime.md` | `8be36a3697bb387a70ead16d53850d21f397dcb57490d7b3b4648994e6ebe54b` |
| `0002-agent-package-contract.md` | `bfe247f5afacb65fa9c07682b7f8818a67807d4bd8042022ae73bbd80f53caea` |
| `0003-authoritative-state-ownership.md` | `5aa2fb997f1761fb59ee37409b1d44c26032b4983113ecf2320da0d6fc99160a` |
| `0004-deterministic-tool-policy.md` | `be9aebe2026b1ae68520ba6f1926643504721642e1fa55e44a7c95bf630d46e0` |
| `0005-layered-memory-governance.md` | `4721a918447ca50f4ba2a527d6d7e7113337bd751f64c46a500ce270d4b06fe0` |
| `0006-durable-workflows-off-speech-path.md` | `15c814fde6605dba3dfedb1acbf115066e5267ad5da4627ba4f59c503ffc9a75` |
| `0007-capability-based-provider-routing.md` | `6a8c4907d4719cce49abeb65adfb44e88198808120e509c28f2cffe06cc7d329` |
| `0008-tenant-isolation.md` | `6aa881d9e06fcada0c3e7aca6f58d220efeaeb7454fc466ca8ac0824e46e6c84` |
| `0009-transactional-deployment-and-rollback.md` | `ef0bdefbd1749aef20730ced5a991b69b8c31e8ab865d7ebb92abf2af7207c91` |

No runtime, installed APK, provider, or live call changed for this milestone.
