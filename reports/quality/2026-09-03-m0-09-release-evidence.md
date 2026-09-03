# M0-09 — Release evidence format

Date: 2026-09-03 UTC  
Status: PASS

## Outcome

PhoneAgent now has one versioned, machine-readable, fail-closed evidence contract for release
decisions. It records exactly eight result categories for every release: tests, evaluation,
benchmark, security, migration, APK, container image, and rollback. A profile policy—not the
producer—decides which categories are mandatory.

The format separates artifact identity from qualification. `release-manifest.json` still identifies
built files; `release-evidence.json` identifies the source, runtime/device/model subjects, execution
environment, gate outcomes, metrics, provenance, redaction state, and immutable evidence behind a
development, candidate, runtime, Android, or combined-production decision.

## Implemented artifacts

- `release/evidence.schema.json`: JSON Schema 2020-12 contract, schema version 1.
- `release/evidence-policy.json`: profile-specific required categories owned by release engineering.
- `release/evidence.py`: strict validator and bundle sealer.
- `docs/RELEASE_EVIDENCE.md`: producer, operator, privacy, schema-evolution, and profile guidance.
- `reports/releases/m0-09-reference/`: closed representative development bundle.
- `tests/test_release_evidence.py`: qualification, mutation, traversal, secret, profile, and schema
  regressions.
- `ci/run-stage.sh quality`: validates the reference bundle on every quality run and emits a JSON
  validation result.

The Python wheel contains the validator, policy, and formal schema. An isolated wheel smoke test
loaded both package resources and successfully validated the committed reference bundle.

## Failure semantics proved

The validator rejects unsupported schema versions, unexpected or missing fields, missing result
categories, producer attempts to weaken profile requirements, required non-applicable results,
failed gates, unowned non-applicability, absent evidence for a passing gate, duplicate or unlisted
files, symlinks, path traversal, incorrect size/hash, broken manifest seal, secret-bearing metadata,
customer-data declarations, duplicate subjects, invalid timestamps, uncommitted candidate or
production source, and passing APK/image gates without matching digest subjects.

The representative bundle honestly remains a `development` profile: tests, evaluation, and
security pass; benchmark, migration, APK, image, and rollback are explicit non-applicable results.
It makes no production, hardware, deployment, rollback, signing, or M0-10 performance claim.

## Final verification

| Gate | Result |
| --- | --- |
| Locked graph | PASS — 223 packages resolved |
| Ruff | PASS — no findings |
| Strict Pyright ratchet | PASS — 0 errors, 0 warnings |
| Frozen WhatsApp boundary | PASS |
| Reference evidence validator | PASS — 8 gates, 3 pass, 5 explicit N/A, 3 artifacts |
| Focused release/CI tests | PASS — 13 passed |
| Conversation evaluation stage | PASS — 153 passed |
| Full non-device suite | PASS — 833 passed, 39 skipped, 4 device tests deselected in 50.20 s |
| Wheel/sdist build and checksum manifest | PASS |
| Built-wheel evidence runtime smoke | PASS |
| Dependency security policy | PASS — one owned, unexpired Pipecat/NLTK exception |

Hashes:

- reference manifest: `sha256:ff2d7cd6f71c1c51407905064625a1019d2207a2e2830b5af1b6711510c1dc08`
- CI validation: `sha256:649306c65b3671a111767bd5c269f02a367f186a96e498aaba93ed7a36d8d022`
- evidence schema: `sha256:06067f83e662db309658b0a0fd61330a39dd29fc6d705c25f06ebbf9eadbe397`
- qualification policy: `sha256:93413c3d25c18978dd8c4b37b8993c60ba848d8e3286076a1e27fcd7f830712f`
- validator: `sha256:592b90777772788e19266f2582ce3670bcb33a7e6f3ad31f66106c607dd7f4d1`
- wheel: `sha256:db9f413bba096b365b3931d1a94df064e5e575a6db755754531f1fdbf8731143`
- dependency audit: `sha256:aa42327020bb47fe3e008f5dfb7fd4d67728fa61e4abea85eaf08cab9f2ee776`

Machine-readable acceptance evidence is in
`reports/quality/2026-09-03-m0-09-evidence.json`; raw CI output is under
`artifacts/ci-m0-09-final/`.

## Deployment and rollback scope

No live container, Android application, phone, external service, persisted production state, or
call was changed. Consequently the reference development bundle records deployment migration,
APK, image, and rollback as non-applicable with exact rationales. Production profiles cannot use
those omissions: their policy requires passing, hash-backed migration/deployment/rollback evidence.
