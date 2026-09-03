# Milestone 1 Task M1-04 Quality Report: S2S Credentials & Dependencies Removal

**Date:** 2026-09-03  
**Task ID:** M1-04  
**Status:** PASS  
**Governing Directive:** docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md & /home/Ubuntu/.codex/skills/phoneagent-cascade-platform/SKILL.md  

---

## 1. Executive Summary

Milestone 1 Task `M1-04` requires removing S2S credentials, authentication caches, and S2S-only package dependencies from the PhoneAgent codebase. With the S2S backend files decommissioned in `M1-03`, their specialized runtime dependencies (`aiortc==1.15.0` and `curl-cffi==0.16.2`) have now been excised from `pyproject.toml`, uninstalled from the active virtual environment, locked in `uv.lock`, and verified through the full CI verification pipeline.

Surviving dependencies (`av==17.1.0` and `websockets==16.1.1`) were evaluated and preserved in accordance with the architectural contract:
- `av`: Required for Cascade real-time Pipecat media pipelines and faster-whisper local STT.
- `websockets`: Retained until M1-08 as defined in `migration/s2s-surface-v1.json`.

---

## 2. Dependency Audit & Deletion

### Removed Dependencies:
1. `aiortc==1.15.0`:
   - Historical usage: WebRTC peer connection management for ChatGPT Realtime API S2S audio stream.
   - Deletion rationale: No Cascade components utilize WebRTC peer connection pipelines; Cascade audio transport operates over low-latency WebSocket/TCP framing to the phone bridge.
   - Status: Removed from `pyproject.toml`, uninstalled via `uv sync`, purged from `uv.lock`.

2. `curl-cffi==0.16.2`:
   - Historical usage: Impersonation-based OAuth authentication & Gizmo session token management for ChatGPT S2S.
   - Deletion rationale: S2S authentication modules were permanently archived to `migration/historical_s2s/`. Cascade uses direct API credentials (`OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY`) and standard `httpx`.
   - Status: Removed from `pyproject.toml`, uninstalled via `uv sync`, purged from `uv.lock`.

### Transitive Package Reductions:
Uninstalling `aiortc` and `curl-cffi` reduced locked packaging complexity, removing transitive packages including `aioice`, `pylibsrtp`, `pyee`, `ifaddr`, and `dnspython`.

---

## 3. Credential and Storage Surface Audit

- **Filesystem Verification:** Checked `~/.config/phone-agent` and project roots. Confirmed zero committed or lingering S2S credentials (`chatgpt_session.json`, `phone_agent_gizmo_cache.json`).
- **Migration Policy:** Cascade voice execution relies exclusively on environment-variable configured API keys or local offline models (`Kokoro`, `Ollama`, `Whisper`). OAuth browser token flows are obsoleted.

---

## 4. Quality & Gate Verification

| Stage | Command | Result | Notes |
|---|---|---|---|
| Dependency Validation | `python3 ci/validate_s2s_inventory.py` | PASS | Asserts `aiortc` and `curl-cffi` are deleted; surviving bindings verified |
| Unit Testing | `./ci/run-stage.sh fast-unit` | PASS | 683 passed, 39 skipped, 1 deselected, 0 failures |
| Quality Gate | `./ci/run-stage.sh quality` | PASS | Ruff, ty, WhatsApp freeze, feature flags, S2S inventory pass |
| Package Gate | `./ci/run-stage.sh package` | PASS | Wheel (`f4d2c8...`) & SDist (`214b5c...`) built cleanly |
| Security Gate | `./ci/run-stage.sh security` | PASS | Pinned audit pass (1 owned torch exception) |
| SBOM Gate | `./ci/run-stage.sh licence-sbom` | PASS | Direct requirement count updated (35 direct, 214 locked) |

---

## 5. Artifacts Produced

- Evidence Bundle: `reports/quality/2026-09-03-m1-04-evidence.json`
- Full Report: `reports/quality/2026-09-03-m1-04-credentials-dependencies.md`
