# Release evidence bundles

Every PhoneAgent release decision uses a closed, machine-readable evidence bundle. A package
manifest proves which files were built; a release evidence bundle separately proves why those
subjects are—or are not—qualified for a specific release profile.

The versioned contract is `release/evidence.schema.json`, qualification policy is
`release/evidence-policy.json`, and the fail-closed reference validator is `release/evidence.py`.
Schema version 1 covers exactly eight result classes: test, evaluation, benchmark, security,
migration, APK, container image, and rollback.

## Bundle layout

```text
<release-id>/
├── release-evidence.json
├── release-evidence.sha256
└── evidence/
    ├── test/...
    ├── evaluation/...
    ├── benchmark/...
    ├── security/...
    ├── migration/...
    ├── apk/...
    ├── image/...
    └── rollback/...
```

Only directories with evidence files need to exist, but `release-evidence.json` always contains one
gate record for every category. Every passing gate has at least one relative, hash-addressed,
size-checked artifact under its category. The validator rejects symlinks, path traversal, missing,
tampered, duplicate, or unlisted files. `release-evidence.sha256` contains only the manifest's bare
SHA-256 and closes the bundle after all producers finish.

## Qualification profiles

| Profile | Mandatory results |
| --- | --- |
| `development` | tests, security |
| `candidate` | tests, evaluation, benchmark, security |
| `production-runtime` | tests, evaluation, benchmark, security, migration, image, rollback |
| `production-android` | tests, security, migration, APK, rollback |
| `production-combined` | all eight categories |

The policy file is authoritative. Required fields in a manifest must exactly match it; producers
cannot weaken a release by setting `required=false`. A required gate must pass. An optional gate may
be `not_applicable` only with a substantive rationale. Failed evidence never qualifies, but the
failed producer artifact should still be retained outside the qualified bundle for diagnosis.

Development evidence may identify a source as `uncommitted` because this project intentionally has
no Git metadata at present, but it must still carry a source-set SHA-256. Candidate and production
profiles require an immutable source revision. Passing APK and image gates also require matching
`android_apk` and `container_image` subjects with `sha256:` digests.

## Producing and verifying a bundle

Evidence producers write sanitized result files first, then write the manifest. Seal and validate:

```bash
uv run python release/evidence.py /path/to/<release-id> --seal
uv run python release/evidence.py /path/to/<release-id>
```

CI validates the committed M0-09 reference bundle on every quality run. Later release orchestration
may assemble the same contract from CI artifacts, hardware qualification, registry attestations,
and deployment controllers; it must not bypass this validator.

## Privacy, provenance, and schema evolution

- Manifest and artifact metadata must declare no customer data. Use only synthetic or redacted
  evidence in portable bundles; store access-controlled real-call evidence by opaque digest.
- Secrets, bearer tokens, passwords, API keys, authorization values, and private keys are forbidden.
- Commands, executor, environment profile, timestamps, source revision/tree digest, subjects,
  metrics, artifact hashes, and media types make every decision attributable and reproducible.
- A new incompatible format gets a new schema version and deterministic migration. Validators never
  silently reinterpret an unknown version.
- Hash closure is tamper evidence, not identity signing. The Milestone 0 exit report has a detached
  Ed25519 signature from a dedicated, environment-local qualification key so that its exact bytes
  can be checked independently; this does not claim publisher identity. Full signed build
  provenance and production trust-root management remain M17-09 requirements.

The representative development bundle is under `reports/releases/m0-09-reference/`. Deployment,
APK, image, benchmark, migration, and rollback entries are explicitly non-applicable there where no
such mutation or M0-10 benchmark occurred; this prevents a development report from masquerading as
production evidence.
