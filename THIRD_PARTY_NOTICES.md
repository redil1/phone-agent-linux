# Third-party notices

This file identifies components whose notices or distribution terms require special handling. The
complete machine-readable review is in `security/dependency-policy.json`; generated release SBOM and
licence inventories remain authoritative for the exact artifact.

## Kokoro 0.9.4 runtime and Kokoro-82M model

PhoneAgent contains modified source from [hexgrad/kokoro](https://github.com/hexgrad/kokoro),
licensed under Apache License 2.0. The full licence and modification provenance ship in
`ai_bridge/_vendor/kokoro/`.

The `hexgrad/Kokoro-82M` model and voice artifacts are licensed under Apache License 2.0 and are
pinned to revision `f3ff3571791e39611d31c381e3a41a3af07b4987`.

## eSpeak NG

The production Linux image installs Debian's `espeak-ng` package
`1.50+dfsg-7+deb11u2`, licensed under GPL-3.0-or-later. PhoneAgent invokes the executable as a
separate process and does not import or link its library. Redistributors must preserve its copyright
and GPL notices and make the corresponding source for the exact distributed package available in the
manner required by the GPL. Debian source-package metadata and the upstream source are available
from [Debian Packages](https://packages.debian.org/bullseye/espeak-ng) and
[espeak-ng/espeak-ng](https://github.com/espeak-ng/espeak-ng).

## NVIDIA CUDA and PhoneLLM base model

The PyTorch `2.13.0+cu129` runtime brings an exact family of CUDA 12.9 NVIDIA wheels, including
CUDA toolkit, bindings, cuBLAS, cuDNN, cuFFT, cuFile, cuRAND, cuSolver, cuSparse, NCCL, NVSHMEM,
NVTX, and related runtime components. They are governed by NVIDIA's CUDA software licence. Preserve
the NVIDIA licence and wheel notices and distribute them only as dependencies of the qualified
PhoneAgent runtime.

PhoneLLM Alpha 1 additionally derives from NVIDIA Nemotron; redistribution must include the NVIDIA
Nemotron Open Model License and the required attribution notice. The quantized model repository
contains both its BSD-2-Clause terms and the NVIDIA terms.

## Dynamically consumed LGPL and MPL components

The release graph includes `edge-tts==7.2.8` under LGPL-3.0-or-later,
`num2words==0.5.14` and `soxr==1.0.0` under LGPL-2.1-or-later, and `tld==0.13.2` under its
MPL-1.1 option. Preserve their notices and source-offer or file-level source obligations, keep LGPL
libraries replaceable, and publish modifications to the covered libraries or files. PhoneAgent does
not relicense these components.

## Supertonic models

The Supertonic Python package is MIT. Downloaded Supertonic model artifacts use OpenRAIL-M; hosted or
redistributed use must retain the licence, flow its use restrictions to users, and enforce the
restricted-use policy.

## Evaluation-only SenseVoice profile

SenseVoice/FunASR is excluded from commercial release installation. Its opt-in evaluation group
contains `kaldiio`, whose NTT licence permits evaluation but forbids third-party distribution. Do not
ship artifacts created with the `sensevoice-evaluation` group.
