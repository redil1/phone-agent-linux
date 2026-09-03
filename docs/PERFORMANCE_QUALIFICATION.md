# Cascade Performance Qualification

The M0 performance harness replays one hash-verified, synthetic, phone-ready fixture through the
only target voice path: STT → LLM → TTS. It measures each provider independently, then measures the
same boundaries end to end. It never dials a device, writes audio to a call, or stores generated
transcripts.

## Profiles

`qualification/performance/profiles.json` is the versioned source of supported environments,
provider/model identities, iteration counts, and release thresholds.

- `linux-x86_64-contract-ci` proves the harness, evidence, privacy, drift, and threshold contracts
  deterministically on ordinary Linux CI. Its synthetic timings are not production performance.
- `linux-x86_64-rtx-a6000-local` exercises the real local stack: Whisper Turbo
  `large-v3-turbo`, Ollama PhoneLLM Alpha 1 Q4_K_M with a 16,384-token runner, and Kokoro-82M
  `af_heart` on the qualified 48 GB RTX A6000 host.

Only profiles registered as `supported` may run. The harness fails closed on an OS, architecture,
GPU, or minimum-VRAM mismatch. Adding a profile requires measured thresholds and model/provider
identity; it is not inferred from a similar machine.

## Measurement contract

- Input is mono, signed 16-bit little-endian PCM at 16 kHz. The corpus manifest and both audio and
  expected-transcript files are SHA-256 verified before execution.
- Cold-start evidence records one process initialization/prewarm observation for every selected
  provider. Warm measurements run after the configured warmup iteration.
- STT records final-transcript latency and word error rate.
- LLM records time to first streamed text token and total completion latency.
- TTS records time to first phone-ready PCM and total synthesis latency; real-time factor is total
  synthesis time divided by generated audio duration.
- End-to-end first-audio latency is `STT final + LLM total + TTS first PCM`. This conservative M0
  boundary does not claim overlap that the replay did not prove.
- Long-call drift compares median LLM TTFT in the first and final quarters of a 60-turn growing
  conversation. Completion-duration samples remain diagnostic only because answer length is not a
  constant workload.
- A provider returning empty text, incomplete streaming output, or no PCM fails the run instead of
  producing a favorable zero measurement.

The report contains hardware/runtime identity, exact providers and options, all samples, p50/p95/
min/max/mean summaries, correctness checks, thresholds, and a machine-readable pass/failure state.
It explicitly declares that it contains no audio, transcripts, or customer data.

## Commands

Run the deterministic release regression:

```bash
uv run python -m phone_agent_gateway.qualification.performance_harness \
  --profile linux-x86_64-contract-ci \
  --output artifacts/performance/contract.json \
  --require-qualified
```

Run the real qualified-host baseline without touching the phone route:

```bash
uv run python -m phone_agent_gateway.qualification.performance_harness \
  --profile linux-x86_64-rtx-a6000-local \
  --output artifacts/performance/rtx-a6000.json
```

Use `--require-qualified` only when a threshold failure must fail the invoking release job. CI runs
the deterministic contract on every evaluation stage; the GPU profile belongs on the self-hosted
performance runner.

## M0 baseline interpretation

The 2026-09-03 RTX A6000 run passes every warm stage target and transcribes the fixed fixture with
zero WER. It fails the long-call drift target: growing dialogue increases LLM TTFT by more than the
15% goal even though absolute late-call TTFT remains below the 400 ms warm target. This is a real,
visible baseline defect—not a reason to weaken the target. M7-04 through M7-12 own stable context
layout, explicit budgets, structured summarization, bounded recent dialogue, cache observability,
GPU admission, and the final 60-minute qualification.

The run also exposed an operational precondition: the shared Hugging Face cache must be writable by
the non-root voice worker. If that ownership drifts, the warm host fails readiness and repeatedly
restarts provider prewarm. Readiness must therefore be checked before measuring or accepting calls.

## Rollback

The harness and profiles are additive. Remove the eval-stage invocation and the
`qualification/performance` package data to roll back M0-10; no persisted customer state, database,
APK, media protocol, phone route, or production configuration is migrated. Benchmark JSON is
evidence and can be retained safely because it contains hashes and metrics only.
