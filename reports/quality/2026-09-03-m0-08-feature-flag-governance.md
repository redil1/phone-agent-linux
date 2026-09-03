# M0-08 — Feature-flag governance evidence

Date: 2026-09-03 UTC  
Status: PASS

## Outcome

PhoneAgent now has a machine-readable registry for temporary flags, transition debt, and durable
boolean controls. Temporary behavior cannot be enabled through an unknown, malformed, or expired
flag. Every temporary flag has an owner, bounded lifetime, rollout and abort criteria, emitted
telemetry, a safe rollback value, source bindings, and a backlog removal target.

The production target and rollback target are both Cascade. The remaining
`PHONE_AGENT_PIPELINE_MODE=s2s_chatgpt_realtime` value is explicitly registered as transition debt,
defaults to `cascade`, becomes unusable after 2026-10-01, and is scheduled for complete removal by
M1-05. No temporary feature flag is permitted to create an alternate speech graph.

## Implemented controls

- `ai_bridge/feature_flags.json` is the authoritative registry: four temporary flags, one bounded
  pipeline transition control, and sixteen classified durable controls.
- `ai_bridge/feature_flags.py` is the fail-closed runtime accessor. It enforces registry/source
  default agreement, documented boolean values, expiration, valid transition values, and Cascade
  fallback after transition expiry.
- `ci/validate_feature_flags.py` scans maintained Python sources, rejects unregistered controls,
  validates ownership/lifecycle/rollback/bindings, verifies declared telemetry exists in runtime
  sources, and rejects any temporary alternate-pipeline effect.
- `ci/run-stage.sh quality` emits `feature-flag-validation.json` on every quality run.
- Runtime and control-plane defaults are now Cascade with speculative and conversational-reflex
  experiments disabled. Supertonic's existing Edge fallback remains explicitly governed.
- Supertonic provider selection/fallback and agent-proposed memory creation now emit exact,
  privacy-safe rollout telemetry.
- The registry is package data and was loaded successfully from the built wheel outside the source
  checkout.
- Operating policy and expiry/removal workflow are documented in
  `docs/FEATURE_FLAG_GOVERNANCE.md`.

## Behavioral proof

`tests/test_feature_flags.py` proves safe defaults, all documented boolean spellings, invalid and
unknown values, source/registry drift, enabled behavior after expiry, disabled rollback after
expiry, S2S transition expiry, continued Cascade operation after expiry, rejection of alternate
pipeline flags, rejection of expired registrations, rejection of invented telemetry, and safe
control-plane defaults. Existing runtime, control-plane, identity, TTS, package, and WebUI tests
also pass.

## Final verification

| Gate | Result |
| --- | --- |
| Locked dependency graph | PASS — 223 packages resolved |
| Ruff | PASS — no findings |
| Strict Pyright ratchet | PASS — 0 errors, 0 warnings |
| Frozen WhatsApp boundary | PASS |
| Feature-control validation | PASS — 4 temporary, 1 transition, 16 durable; 0 alternate-pipeline flags |
| Full non-device suite | PASS — 825 passed, 39 skipped, 4 device tests deselected in 49.46 s |
| Source and wheel package | PASS — built and checksum manifest verified |
| Built-wheel registry runtime smoke | PASS |
| Dependency security policy | PASS — one known Pipecat/NLTK exception remains owned and unexpired |

Hashes:

- registry: `sha256:54b9d0485ef2993b88d644d066728291da8588ad3bd43d8813ff4ab689c6f1a3`
- validation artifact: `sha256:0386d358b1568674d7b85a502cc19641fe359066ee97e27db9cc25e37fa364dc`
- wheel: `sha256:5ef2c473c51d579da25f1162addf6a57ba61de630a2ebfb4399cf2138da844d1`
- release manifest: `sha256:ea404b0b1f289e117feab3ff4e9dd3f69ba7bb91f7bd41df51955a81c1552d0d`
- dependency-audit artifact: `sha256:aa42327020bb47fe3e008f5dfb7fd4d67728fa61e4abea85eaf08cab9f2ee776`

Machine-readable evidence is in `reports/quality/2026-09-03-m0-08-evidence.json`; raw CI outputs
are under `artifacts/ci-m0-08-final/`.

## Deployment and rollback scope

No production container, Android application, phone state, external service, or live call was
changed. This item changes the next built runtime. Rollback is configuration-only for temporary
flags using each registered `safe_value`; the pipeline transition always rolls back toward
`cascade`. The previously qualified GSM/Android baseline remains untouched.
