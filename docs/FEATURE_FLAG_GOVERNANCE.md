# Feature-flag governance

PhoneAgent has one target production voice graph: streaming STT → agent runtime/LLM → streaming
TTS. A temporary flag may change behavior inside that graph, choose a compatible provider fallback,
or govern agent state. It may not introduce another permanent voice pipeline.

`ai_bridge/feature_flags.json` is the authoritative registry. CI scans Python environment-control
call sites and fails when a temporary flag, transition control, or durable boolean control is not
registered. Every temporary flag must declare its owner, purpose, allowed values, default, rollout
stages, success and abort criteria, telemetry, expiration, rollback procedure, source bindings, and
backlog removal target. Maximum registration lifetime is 120 days.

## Control classes

- **Temporary flag:** a bounded experiment or migration fallback. Runtime access is only through
  `feature_flag_enabled`; unknown values and enabled expired behavior fail closed.
- **Transition control:** bounded compatibility debt. `PHONE_AGENT_PIPELINE_MODE` defaults and
  rolls back to `cascade`; its legacy S2S value becomes invalid after 2026-10-01 and its entire
  branch is removed under M1-05.
- **Durable control:** security authorization, consent, topology, provider behavior, or operator
  policy—not a rollout mechanism. It is inventoried so it cannot be disguised as an experiment.

## Release procedure

1. Add the registry entry and runtime binding together. Default to the existing safe behavior.
2. Prove synthetic evaluation before internal canary activation. Record the registry name and value
   with latency, reliability, semantic-quality, and abort telemetry.
3. Advance `current_stage` only with release evidence. Abort immediately when its declared criteria
   fire and apply the registered safe value.
4. Before `expires_on`, either graduate the behavior by deleting the branch and flag, or remove it.
   Extending an expiry is a new reviewed decision, not routine maintenance.
5. CI rejects an expired registry or a temporary flag whose pipeline effect could create an
   alternate speech graph. Cascade remains usable after expiry at the registered safe value.

## Current bounded controls

| Control | Safe default | Expires | Removal |
| --- | --- | --- | --- |
| `PHONE_AGENT_SPECULATIVE_PIPELINE` | false | 2026-11-30 | M2-07 |
| `PHONE_AGENT_CONVERSATIONAL_REFLEX` | false | 2026-11-30 | M4-05 |
| `PHONE_AGENT_SUPERTONIC_FALLBACK_TO_EDGE` | true | 2026-11-30 | M2-08 |
| `PHONE_AGENT_IDENTITY_PROPOSALS_ENABLED` | false | 2026-11-30 | M8-07 |
| `PHONE_AGENT_PIPELINE_MODE` transition debt | cascade | 2026-10-01 | M1-05 |

The registry, not this table, is machine-authoritative. This document explains the operating policy;
the CI validation artifact proves conformance for each release.
