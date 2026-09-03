# ADR-0001 — One Cascade voice runtime

Status: Accepted — implementation in transition

Date: 2026-09-02

## Context

Two voice pipelines created configuration drift, duplicated conversation logic, inconsistent tool
and memory behavior, and failures that appeared only after switching Studio settings. The qualified
Android/GSM PCM transport is valuable; the competing speech-to-speech control path is not.

Current conformance: the verified production selection is Cascade, but
`ai_bridge/chatgpt_realtime_pipeline.py`, `ai_bridge/openai_realtime_websocket_pipeline.py`, S2S
configuration fields, tests, and Studio branches remain executable transition debt.

## Decision

PhoneAgent has one production voice topology: authenticated PCM input → authoritative turn
controller/STT → universal agent runtime/LLM → streaming TTS → authenticated phone playout and
acknowledgement. S2S is forbidden production architecture. Provider choice may vary inside STT,
LLM, and TTS adapters but cannot create another conversation authority or bypass structured state,
tools, policy, evaluation, or delivery verification.

## Invariants

- Only authoritative final caller turns enter durable or consequential state.
- The model cannot own call transport, authorization, media integrity, or completion evidence.
- Transcript generation is not delivery; phone playout acknowledgement is delivery.
- Android/GSM protocol behavior remains characterized and qualified throughout removal.
- There is one live conversational authority per call.

## Alternatives considered

- Keep permanent Cascade and S2S modes: rejected because behavior and safety inevitably diverge.
- Prefer S2S for latency: rejected because it couples audio, state, tools, and provider behavior and
  cannot meet the universal, provider-neutral contract.
- Create task-specific pipelines: rejected because product, persona, and task are configuration.

## Consequences

All shared agent capability must live in provider-neutral Cascade components. S2S-only code and
credentials are deleted after equivalent characterization coverage exists. Low latency must be
earned through streaming, prewarm, bounded context, routing, and turn intelligence.

## Migration and rollback

Milestone M1-01 inventories the S2S surface; M1-02 characterizes shared behavior before deletion;
M1-05 migrates saved settings; M1-10 proves installed upgrade and rollback. Active calls remain on
their starting runtime. Rollback never means re-enabling S2S; it means restoring the last qualified
Cascade release and its compatible saved state.

## Verification

The controlling plan is `docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md`. M1-09 adds a repository,
schema, UI, and runtime invariant test; the Milestone 1 exit gate requires a qualified GSM call.
Baseline evidence is in `reports/baselines/2026-09-02-gsm-cascade-baseline.*`.

## Supersession

Only a later ADR with measured safety, universality, latency, migration, and rollback evidence can
supersede this decision. A new provider or modality alone is insufficient.
