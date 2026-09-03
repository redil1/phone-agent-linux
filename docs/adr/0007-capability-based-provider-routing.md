# ADR-0007 — Capability-based provider routing within Cascade

Status: Accepted — implementation in transition

Date: 2026-09-02

## Context

Provider names currently leak into configuration, class names, telemetry, and behavior. Silent
fallbacks can change language, latency, context capacity, voice, tool support, or data residency.
Universal agents need replaceable STT, LLM, and TTS without semantic drift.

Current conformance: provider adapters, prewarm, model selection, native Ollama telemetry, and some
fallback checks exist. The Whisper wrapper is misleadingly named Parakeet, S2S fields remain, and a
complete capability registry/conformance suite/router does not.

## Decision

Routing occurs only among typed STT, LLM, and TTS adapters inside the single Cascade runtime.
Providers publish capabilities, languages, regions, data-handling class, context/stream limits,
health, warm state, latency/cost observations, cancellation behavior, and exact model/voice digest.
The compiler creates an allowed route set; the runtime selects from it using policy and health. A
fallback must be semantically compatible and observable or the turn fails honestly.

## Invariants

- No provider bypasses authoritative turn, agent, policy, tool, state, or playout components.
- Model/voice switches happen only at declared safe boundaries and are visible by digest.
- Provider failures cannot silently reduce required language, tool, context, privacy, or voice
  capability.
- Conformance fixtures and latency metrics use provider-neutral semantics.
- Context growth is bounded independently of provider window size.

## Alternatives considered

- Hard-code one provider: rejected for availability, sovereignty, cost, and deployment diversity.
- Catch any failure and choose any configured provider: rejected because semantic incompatibility is
  worse than an explicit failure.
- Route via provider-specific prompts: rejected because behavior must compile from the package.

## Consequences

Adapters must implement stronger contracts and health telemetry. Operators gain predictable
failover, explainability, benchmarkable latency, and the ability to support local or cloud profiles
without forking the agent.

## Migration and rollback

M1-08 removes provider assumptions from shared runtime; M2-03 validates compatibility; M7-10 and
M7-11 implement capability routing and conformance. Legacy provider names map to registry entries.
Rollback restores the previous compiled route set and pinned warm worker.

## Verification

`docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md` M7-09 through M7-12 and provider suites in M6/M8 are
controlling. Runtime read-back must expose selected provider/model and reject incompatible fallback.

## Supersession

A router or registry replacement must preserve compiler-bounded routes, semantic compatibility,
exact identity, safe boundaries, and observable failure. Provider market share is not a reason to
weaken the contract.
