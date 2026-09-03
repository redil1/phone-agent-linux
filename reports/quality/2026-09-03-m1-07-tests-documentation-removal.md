# Milestone 1 Task M1-07 Quality Report: Remove S2S Tests and Documentation

**Date:** 2026-09-03  
**Task ID:** M1-07  
**Status:** PASS  
**Governing Directive:** docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md & /home/Ubuntu/.codex/skills/phoneagent-cascade-platform/SKILL.md  

---

## 1. Executive Summary

Milestone 1 Task `M1-07` requires removing Speech-to-Speech (S2S) specific test modules once replacement coverage exists and updating documentation so that no misleading capability claims remain.

All S2S-specific tests have been removed from the active test suite and safely preserved in `migration/historical_s2s/tests/`. Operational and installation documentation has been rewritten to reflect Universal Cascade as the sole voice pipeline architecture.

---

## 2. Test & Documentation Removals

1. **Excised Test Files (archived in `migration/historical_s2s/tests/`):**
   - `tests/test_chatgpt_gizmo_manager.py`
   - `tests/test_chatgpt_realtime_audio.py`
   - `tests/test_chatgpt_realtime_auth.py`
   - `tests/test_chatgpt_realtime_pipeline.py`
   - `tests/test_openai_realtime_websocket_pipeline.py`

2. **Inventory & Policy Tracking:**
   - Updated `ci/validate_s2s_inventory.py` to record `DELETED_TESTS` and `REWRITTEN_DOCS`.

3. **Documentation Rewrites:**
   - `docs/WEBUI_USER_GUIDE.md`: Replaced S2S pipeline references with Universal Cascade (STT → LLM → TTS).
   - `docs/NEW_MAC_INSTALL_GUIDE.md`: Updated prerequisites and architecture description to Universal Cascade.
   - `docs/TOOLS_AND_MCP.md`: Updated agent runtime context to Universal Cascade.

---

## 3. CI Gate Verification

| Stage | Command | Result | Details |
|---|---|---|---|
| Fast Unit | `./ci/run-stage.sh fast-unit` | PASS | 683 passed, 39 skipped, 1 deselected |
| Quality | `./ci/run-stage.sh quality` | PASS | Ruff, ty (0 errors, 0 warnings), WhatsApp freeze, feature flags, S2S inventory pass |
| Package | `./ci/run-stage.sh package` | PASS | Wheel (`bf2d93...`) and SDist (`d163af...`) verified |
| Security | `./ci/run-stage.sh security` | PASS | Pip-audit passes with 1 owned torch exception |
| SBOM | `./ci/run-stage.sh licence-sbom` | PASS | Dependency and licensing inventory verified |

---

## 4. Evidence Artifacts

- Evidence Bundle: `reports/quality/2026-09-03-m1-07-evidence.json`
- Quality Report: `reports/quality/2026-09-03-m1-07-tests-documentation-removal.md`
