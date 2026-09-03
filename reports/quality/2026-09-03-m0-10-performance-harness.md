# M0-10 — Cascade performance harness

Date: 2026-09-03 UTC  
Implementation status: PASS  
RTX A6000 profile qualification: PARTIAL — long-call drift remains above target

## Outcome

PhoneAgent now has a packaged, versioned performance harness for the sole target pipeline,
STT → LLM → TTS. It replays hash-verified synthetic 16 kHz mono PCM through each real provider
boundary and through the composed Cascade, emits metadata-only JSON, and fails closed on profile,
fixture, environment, provider-output, or threshold errors.

The deterministic Linux contract runs in every CI evaluation stage. The real A6000 profile uses
Whisper Turbo `large-v3-turbo`, Ollama
`hf.co/EryriLabs/phonellm-alpha-1-GGUF:Q4_K_M` at `num_ctx=16384`, and Kokoro-82M `af_heart`.
It records cold initialization, warm p50/p95/min/max/mean stage latency, STT WER, LLM TTFT and total,
TTS TTFA/total/real-time factor, conservative end-to-end TTFA, correctness, hardware/runtime
identity, all raw timing samples, and 60-turn TTFT drift.

## Real measured baseline

Host: Intel Xeon Gold 6238R, 48,268 MiB reported RAM, NVIDIA RTX A6000 49,140 MiB, driver
580.126.20. The candidate container used Python 3.11.13. No phone call or Android media route was
used.

| Signal | A6000 result | Target | Result |
| --- | ---: | ---: | --- |
| STT final p95 | 146.183 ms | ≤ 300 ms | PASS |
| LLM TTFT p95 | 142.703 ms | ≤ 400 ms | PASS |
| TTS first PCM p95 | 158.187 ms | ≤ 200 ms | PASS |
| Cascade first PCM p95 | 615.549 ms | ≤ 1,000 ms | PASS |
| TTS RTF p95 | 0.04276 | informational | PASS |
| STT WER max | 0.00000 | ≤ 0.15 | PASS |
| 60-turn LLM TTFT drift | 26.556% | ≤ 15% | FAIL |

The late-quarter LLM TTFT median was 268.931 ms versus 212.500 ms in the early quarter. Absolute
latency remains below the warm TTFT target, but the drift target is intentionally unchanged. M7-04
through M7-12 own the stable context layout, budget, structured summary, bounded recent dialogue,
cache observability, GPU admission, and final 60-minute qualification required to close this gap.

Cold process initialization was 18,126.683 ms: STT 5,861.776 ms, already-resident LLM 13.602 ms,
and TTS 12,248.327 ms. These loads occur before readiness; warm call-stage latency is the release
SLO. The real run produced eleven complete LLM responses, eleven non-empty PCM outputs, and zero
fixture transcription errors.

## Production defects found and contained

The first real run exposed two independently reproducible issues:

1. A root-owned shared Hugging Face cache directory prevented the non-root warm voice worker from
   creating its cache. The worker repeatedly failed readiness and retried provider initialization.
   Ownership of exactly `/home/Ubuntu/.cache/huggingface` was restored to UID/GID 1001. The worker
   then became ready and remained configuration-current with Cascade, Whisper Turbo, PhoneLLM, and
   Kokoro. No call was placed.
2. The legacy GPU preloader warmed Ollama without the configured runner options. Ollama therefore
   alternated 16,384- and 8,192-token runners for the same 23 GiB model, producing 18–37 second
   reloads and one 60-second timeout. `ollama_runtime_options()` now gives every warmup path the
   exact live runner shape and model-required sampling overrides. The profile is aligned to the
   production 16,384-token runner and has a bounded 120-second cold-load timeout.

The final model prewarm measured 13.602 ms, proving it hit the resident matching runner rather than
reloading weights. The live warm host remained ready and configuration-current after both full
benchmarks.

## Verification

| Gate | Result |
| --- | --- |
| Locked dependency graph | PASS — 223 packages |
| Ruff | PASS |
| Strict Pyright | PASS — 0 errors, 0 warnings |
| Focused performance/CI/runtime/Ollama tests | PASS — 59 passed |
| CI evaluation stage | PASS — 153 tests plus contract benchmark |
| Contract profile | PASS — all six checks, 60 turns |
| Real A6000 profile | THRESHOLD FAILURE — five checks pass; long-call drift fails |
| Full non-device suite | PASS — 890 passed, 40 skipped, 4 device tests deselected in 49.45 s |
| Wheel and sdist with checksum manifest | PASS |
| Installed-wheel performance resource smoke | PASS |
| Dependency security policy | PASS — one owned, unexpired Pipecat/NLTK exception |
| Licence/SBOM policy | PASS — 223 locked packages, 181 SBOM components |

Primary hashes:

- harness: `sha256:31c0ecce87596ece8be691d196543b744eb0057e4489e55d97697ce895b33584`
- profile registry: `sha256:c3cc9c085058174d70c1712e71ec89c97ad76398252fd5803917e7d1ec13dd04`
- contract artifact: `sha256:eb106b19ca732a0bff111f54dde2787ae89fc3cab1e3a6d2f0708e8933be053b`
- A6000 artifact: `sha256:e0d94dcd2ad176d646a4dba4353f88d4f3fd34dac30071d13f6018800fc40303`
- built wheel: `sha256:37ad06ca0648561d9e5bf7ce1ddf31649164723e86fc625288bab96bf87023d8`
- dependency audit: `sha256:94d0fed2f9d55179c9165fcbd9150b274d8317cd9025c35b4b0e035ef06c89d8`

## Scope and rollback

No APK, GSM protocol, persisted schema, call route, provider selection, model selection, or live
container image was changed. The only live repair was ownership of one cache directory required by
the existing non-root worker. M0-10 source rollback removes the eval invocation, harness, profile
data, and runner-shape helper; it requires no data migration. The benchmark artifacts contain no
audio, transcript, secret, customer identifier, or customer data.
