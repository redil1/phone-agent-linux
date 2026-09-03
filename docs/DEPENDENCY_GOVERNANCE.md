# Dependency governance

PhoneAgent releases are built from exact, reviewable dependency graphs. This policy covers Python,
Rust, Android, container bases, CUDA, and model artifacts; it applies equally to local builds and CI.

## Reproducible graph

- Every direct Python requirement is exact. Registry and URL artifacts are hash-locked in
  `uv.lock`; direct URL requirements also carry their SHA-256 in `pyproject.toml`.
- `uv.lock` is the transitive Python authority. Production uses Python 3.11.13, PyTorch
  `2.13.0+cu129`, and the explicit CUDA 12.9 PyTorch index. Unused torchaudio was removed from the
  commercial graph.
- `Cargo.lock` is the Rust authority. The WhatsApp dependency additionally uses an immutable Git
  revision. Build and test commands must use `--locked`.
- The Python runtime and uv installer image use immutable OCI digests. Python and all native
  extensions stay on Debian Bullseye to prevent cross-distribution ABI drift. Debian packages
  resolve from the immutable `20250721T000000Z` snapshot and every direct APT package has an exact
  version. CUDA user-space libraries come from hash-locked PyTorch cu129 wheels; the NVIDIA
  container runtime injects only the host driver interface. The production Dockerfile
  installs Python exclusively with `uv sync --frozen`; ad-hoc `pip install` commands are forbidden.
- Android qualification uses Java 17, compile SDK 34, and build tools 34.0.0. A toolchain change is
  a qualification event, not an automatic update.
- Models are not ordinary packages. Their repository revision, artifact hash, licence, GPU profile,
  latency, and quality evidence belong in each release manifest.

The machine-readable authority is `security/dependency-policy.json`.

## Update and vulnerability handling

A named owner reviews the graph every 14 days, refreshes transitives at least every 30 days, and
reviews toolchains every 90 days. Critical advisories are triaged within 24 hours and remediated
within 72 hours; high advisories are triaged within 72 hours and remediated within 14 days.

An exception is never a generic package allowlist. It must match an advisory identifier, exact
package version, owner, rationale, and expiry. The maximum exception lifetime is 90 days. CI rejects
new findings, drift, expiry, and stale exceptions. The current NLTK exception remains explicit while
upstream has no fixed release; it does not authorize other findings or versions.

The hash-locked `en-core-web-sm==3.8.0` URL artifact is excluded from the pip-audit input because
pip-audit 2.10.1 rejects direct URL requirements. It is not invisible: its exact hash is enforced by
the dependency validator, its MIT decision is recorded, and it remains in the generated CycloneDX
SBOM and licence inventory.

PyTorch remains installed and verified as `2.13.0+cu129`. For advisory lookup only, the generated
audit manifest maps that CUDA build label to upstream source version `2.13.0`, the
version used by public vulnerability databases. The mapping is exact, machine-readable, and the
gate fails if it no longer matches the release graph.

This upgrade removes the PyTorch 2.6 findings from the commercial graph. Its exact CUDA 12.9 wheel
dependencies include NVIDIA's `cuda-toolkit==12.9.1` and `cuda-bindings==12.9.7`; both are recorded
as conditional commercial components with notice and distribution obligations.

Every update includes a lock diff, CVE report, licence report, SBOM, tests, and rollback artifact.
Media/model changes additionally require latency and quality comparison against the protected call
corpus.

## Commercial licence release rule

Permissive licences on the technical allowlist can progress through engineering review while
preserving their notices and attribution. LGPL, NVIDIA terms, and any other conditional licence need
their obligations recorded for the actual distribution. GPL/AGPL, unknown, and evaluation-only
components cannot enter a proprietary release without written approval from the product owner or
qualified counsel.

This engineering policy does not replace legal advice. It deliberately fails closed when the
distribution model is uncertain.

The initial review found two genuine blockers and removed both from the commercial Python graph:

- `kaldiio==2.18.1`, pulled by FunASR/SenseVoice, is covered by an NTT evaluation-only agreement.
  SenseVoice and its dependencies now live only in the `sensevoice-evaluation` group, which is not
  exported, installed, or audited as a commercial release dependency.
- Kokoro's former Python G2P path installed `phonemizer-fork` under GPLv3+ and
  `espeakng-loader`, whose MIT wrapper bundles GPLv3+ eSpeak binaries. PhoneAgent now vendors the
  audited Apache-2.0 Kokoro 0.9.4 runtime, pins the Apache-2.0 model revision, and invokes Debian's
  eSpeak executable as a separate process. The old in-process packages are absent from the release
  graph. GPL notices and corresponding-source access for the separate executable remain mandatory.

The previously unknown `google-crc32c==1.8.0` is Apache-2.0. The unknown CUDA 13 meta-package was
not approved; pinning the qualified PyTorch CUDA 12.9 index removed it from the graph. PhoneAgent's
own package now declares the conservative `LicenseRef-Proprietary` expression without granting
third-party rights.

The model register fixes and reviews the exact revisions of Whisper, Kokoro, PhoneLLM, Supertonic,
and SenseVoice. OpenRAIL and NVIDIA model terms remain explicit distribution obligations; the
SenseVoice model and runtime are evaluation-only.

## Update procedure

1. Open a change for one dependency class and name its owner.
2. Update only through the class-specific command in `security/dependency-policy.json`.
3. Review the complete lock diff; never hand-edit a lockfile.
4. Generate CVE, licence, and CycloneDX reports and resolve every new or changed decision.
5. Run the relevant CI stages and media/model regression gates.
6. Record the image/model/APK hashes and rollback artifact in release evidence.
7. Merge only when all release blockers are closed and the prior artifact remains deployable.
