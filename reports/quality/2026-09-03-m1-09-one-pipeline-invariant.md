# Milestone 1 Task M1-09 Quality Report: One-Pipeline Invariant Test

**Date:** 2026-09-03  
**Task ID:** M1-09  
**Status:** PASS  
**Governing Directive:** docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md & /home/Ubuntu/.codex/skills/phoneagent-cascade-platform/SKILL.md  

---

## 1. Executive Summary

Milestone 1 Task `M1-09` requires adding an automated test suite enforcing the one-pipeline invariant: CI must fail if a new speech-to-speech runtime, mode, service, or UI selector is introduced. Universal Cascade must remain the sole real-time voice pipeline architecture.

The invariant test suite has been implemented in `tests/test_one_pipeline_invariant.py` and validated across all CI stages.

---

## 2. Invariant Enforcements

The test suite `tests/test_one_pipeline_invariant.py` enforces three critical invariants:

1. **Runtime Rejection Invariant (`test_pipeline_mode_strictly_rejects_legacy_and_non_cascade`):**
   - Asserts that legacy S2S mode raises an explicit `ConfigurationError` instructing migration to Cascade.
   - Asserts that any alternative or newly introduced pipeline mode other than `cascade` fails validation.

2. **Studio UI Invariant (`test_ui_contains_no_legacy_selectors_or_options`):**
   - Asserts that `ai_bridge/web_static/index.html` contains no S2S selector blocks, transport selectors, model dropdowns, reasoning dropdowns, or voice dropdowns.

3. **Production Source Tree Invariant (`test_no_forbidden_legacy_runtime_modules_in_production_tree`):**
   - Asserts that deleted S2S runtime files (`chatgpt_gizmo_manager.py`, `chatgpt_realtime_auth.py`, `chatgpt_realtime_pipeline.py`, `openai_realtime_websocket_pipeline.py`) do not exist in `ai_bridge/`.

---

## 3. Verification & Gate Results

| Stage | Command | Result | Details |
|---|---|---|---|
| Fast Unit | `./ci/run-stage.sh fast-unit` | PASS | 686 passed, 39 skipped, 1 deselected |
| Quality | `./ci/run-stage.sh quality` | PASS | Ruff, ty (0 errors, 0 warnings), WhatsApp freeze, feature flags, S2S inventory pass |
| Package | `./ci/run-stage.sh package` | PASS | Wheel (`79f545...`) and SDist (`83fc20...`) verified |
| Security | `./ci/run-stage.sh security` | PASS | Pip-audit passes with 1 owned torch exception |
| SBOM | `./ci/run-stage.sh licence-sbom` | PASS | Direct requirement count 35, locked packages 214 |

---

## 4. Evidence Artifacts

- Evidence Bundle: `reports/quality/2026-09-03-m1-09-evidence.json`
- Full Quality Report: `reports/quality/2026-09-03-m1-09-one-pipeline-invariant.md`
