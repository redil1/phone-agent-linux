# M0-07 — Dependency governance

Date: 2026-09-02 UTC

## Outcome

M0-07 passes. The commercial Cascade image is generated from one immutable Python graph, immutable
container bases, a frozen Debian snapshot, exact system packages, exact model revisions, and an
enforced licence/CVE policy. The policy reports no commercial release blocker.

The qualified candidate is `phoneagent-cascade:m0-07-candidate`, image ID
`sha256:4ff4350b3c252ac182240d9f57a87544ecf139bae9890ba9be4c4bed7a86a386`
(9,072,579,170 bytes). It was not deployed over the running production container.

## Locked release graph

- All 37 direct Python requirements are exact; URL artifacts carry SHA-256 fragments.
- `uv.lock` fixes 223 packages and hashes every registry, URL, or Git artifact.
- Production is restricted to Python 3.11 and uses the immutable Python 3.11.13 Bullseye image.
- PyTorch is `2.13.0+cu129` from the explicit official cu129 index. Moving from 2.6/cu124 removed
  23 PyTorch vulnerability findings. Unused torchaudio was removed.
- CUDA user-space libraries are the exact, hash-locked wheel graph. The NVIDIA runtime injects the
  host driver interface. CTranslate2 receives explicit cuBLAS and cuDNN library paths.
- Debian packages resolve only through snapshot `20250721T000000Z`; all ten direct APT packages are
  exact. Runtime inspection matched the requested versions.
- Production sync is frozen and contains no ad-hoc pip install. BuildKit's Dockerfile check reports
  no warning.

## Vulnerability policy and result

The security stage creates both the immutable hashed deployment manifest and a separate advisory
lookup manifest. The only normalization is the governed mapping from
`torch==2.13.0+cu129` to source release `torch==2.13.0`; the gate fails if that exact mapping stops
matching the lock. The hash-locked `en-core-web-sm==3.8.0` URL artifact remains in the SBOM and
licence gate but is omitted from pip-audit because pip-audit 2.10.1 rejects URL requirements.

Pip-audit examined 180 registry distributions. Its only finding is `PYSEC-2026-3740` /
`CVE-2026-81726` in Pipecat's transitive `nltk==3.10.3`. Upstream has no fixed release. PhoneAgent
does not call the affected NLTK model path APIs or accept caller-controlled NLTK paths. The exact
exception is owned, version-bound, and expires 2026-12-01; expiry, drift, or a new finding fails CI.

## Commercial licence result

The generated inventory contains 177 installed distributions. Both metadata-unknown records are
resolved by exact evidence:

- `google-crc32c==1.8.0` is Apache-2.0.
- `cuda-toolkit==12.9.1` is governed by the NVIDIA CUDA software licence and recorded obligations.

The 15 exact NVIDIA runtime wheels are governed as one versioned component family. CUDA bindings,
edge-tts, num2words, soxr, and tld have explicit conditional decisions and distribution
obligations. Five model revisions and the Debian eSpeak executable have named owners, immutable
evidence, and distribution decisions. The CycloneDX 1.5 SBOM validates with 181 components and 182
dependency nodes. The machine gate reports zero commercial blockers.

Two previously hidden blockers were removed from the commercial graph:

- SenseVoice/FunASR remains an opt-in evaluation group because `kaldiio==2.18.1` uses an
  evaluation-only NTT licence. It is absent from production installation and export.
- The old Kokoro path pulled GPL `phonemizer-fork` and bundled eSpeak through
  `espeakng-loader`. PhoneAgent now vendors the audited Apache-2.0 Kokoro 0.9.4 Python runtime and
  invokes Debian's eSpeak executable across a shell-free, bounded subprocess boundary. The two old
  Python packages are absent from the lock and image.

The engineering decision record and required notices are in `security/dependency-policy.json` and
`THIRD_PARTY_NOTICES.md`. Final legal approval remains the responsibility of the product owner or
qualified counsel.

## Defects found by artifact qualification

1. Copying Python from Debian into an Ubuntu CUDA base produced unresolved libffi and OpenSSL ABIs.
   The final image keeps Python and native extensions on one immutable Debian distribution.
2. One vendored Kokoro module retained an absolute `kokoro` import. It now uses the package-relative
   import and succeeds without the removed upstream package.
3. CTranslate2 could not discover CUDA wheel libraries. The final image exposes only the governed
   cuBLAS/cuDNN directories plus NVIDIA's driver injection paths.
4. The standalone command forced an external bind while the application correctly required an
   explicit external-binding opt-in. Standalone now defaults to loopback; production Compose keeps
   its explicit external opt-in.
5. The security stage treated a missing pip-audit JSON file as a vulnerability exit. It now fails
   operationally when the required report is absent and uses a deterministic advisory manifest.

## Exact artifact runtime proof

The final image ran as non-root on the NVIDIA RTX A6000 with Python 3.11.13, PyTorch
`2.13.0+cu129`, and CUDA runtime 12.9. None of Kokoro, espeakng-loader, phonemizer-fork, FunASR,
ModelScope, kaldiio, or torchaudio was installed.

| Gate | Result |
| --- | --- |
| Default Cascade speech stack | Whisper Turbo `large-v3-turbo` and Kokoro |
| Fresh Kokoro/model prewarm | 15,672.9 ms including fresh artifact download and model load |
| Warm Kokoro synthesis | 145.2 ms for 4,300 ms / 137,600 bytes of 16 kHz PCM |
| Fresh Whisper load | 6,993.4 ms including fresh artifact download and model load |
| Whisper inference | 1,033.4 ms; exact expected English transcript; language probability 0.998 |
| Standalone control plane | `/api/status` returned `status=ok`; identity evaluation and production readiness true |
| Focused governance/Kokoro/CI tests | 24 passed |
| Full non-device regression | 856 passed, 40 skipped, 4 device tests deselected; 52.33 s |
| Ruff / strict Pyright / frozen WhatsApp | Pass / zero diagnostics / pass |

The model cache was deliberately rebuilt in `/tmp` for the final run, so the timings distinguish
cold artifact acquisition and loading from warm inference. No phone call was placed.

## Reproducibility hashes

| Artifact | SHA-256 |
| --- | --- |
| `pyproject.toml` | `5ff4ff22309b0ff15b15602772a22b931ebdb6e183b6b9460f9c327d843ad800` |
| `uv.lock` | `7ceaaa4989cc051d69f63bb29569d13b95e8d935b2bf0a24a0f587c602f72e08` |
| `Dockerfile.cuda` | `82d9b4e530e2df8cf33619e4717f99a27c7c81a692bf377465ff1987fe53a88d` |
| `security/dependency-policy.json` | `ee4c4bd6c97e70fb98cbbf9654bfd34ec2f860762d60006682630c2c3920e2fd` |
| `security/dependency-audit-allowlist.json` | `43b365c707e8bfe271ebfa2d724a15d498f4d5335185bcbf88399cd0c162267c` |
| `ci/run-stage.sh` | `cdcdb8190e5cb5e36ac53448e1b24edae5cbc1e37a57300f0974cb2e7f439188` |
| `ci/prepare_audit_requirements.py` | `7029c98b7b9ec215f971c422d6ced6ff8e6c42abef77a4ac446e8d03bd8a9172` |
| `ci/validate_dependency_policy.py` | `6d5717bf71ef63f32f5eb3c3771bddf06ae733d23378933246fb36ecc2fccb9a` |
| Generated vulnerability report | `94d0fed2f9d55179c9165fcbd9150b274d8317cd9025c35b4b0e035ef06c89d8` |
| Generated vulnerability validation | `43d357cf5f634f0907cda21d1d2579b38546d75f8aa1ba0e7658c9ddd4126f29` |
| Generated CycloneDX SBOM | `2ab833914e7f6507fe8f779441e24110d806ff4124221e9ca0ba3a8539d699c3` |
| Generated licence inventory | `2c2ef9da76766715fd27ef38a5530a97a566603901f5e59d8e009f9ba8b3ddcb` |
| Generated dependency policy validation | `5f8821191f62338b211a0ef20e3c58cc82f821eebeaa685252d70345b9eae939` |
| Final GPU STT/TTS proof JSON | `78fd53828b9fc73df87ba82cc0c7fc789a6e30934803651ffc1d3149e85b1ed6` |
| Final control-plane status JSON | `d8e17b495a5c3d03955bbe901cf5ab62b9f027bd8c8491fcad7363224ade0db2` |

No production service was restarted, no Android package or device was changed, and no live call was
placed. Obsolete M0-07 candidate images, their recoverable build cache, the generated local virtual
environment, and the two temporary smoke containers were removed after use; the live image and
container were preserved.
