# Milestone 1 Task M1-06 Quality Report: Remove S2S Studio Controls

**Date:** 2026-09-03  
**Task ID:** M1-06  
**Status:** PASS  
**Governing Directive:** docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md & /home/Ubuntu/.codex/skills/phoneagent-cascade-platform/SKILL.md  

---

## 1. Executive Summary

Milestone 1 Task `M1-06` requires stripping all Speech-to-Speech (S2S) controls from the Studio web frontend, displaying exclusively modular Universal Cascade controls (STT, LLM, TTS, turn detection, voice, and telephony runtime settings).

All S2S controls and parameters have been cleanly excised from `ai_bridge/web_static/index.html`. Associated web server tests were updated and validated across all CI stages.

---

## 2. Studio UI Adjustments

1. **HTML Template Updates:**
   - Excised `div#chatgpt-realtime-options` including:
     - Realtime Model selector (`#chatgpt-realtime-model`)
     - S2S Transport selector (`#chatgpt-realtime-transport`)
     - Realtime Reasoning Effort selector (`#chatgpt-realtime-reasoning`)
     - S2S Turn Detection selector (`#chatgpt-realtime-vad-mode`)
     - ChatGPT Realtime Voice selector (`#chatgpt-realtime-voice`)
     - Historical S2S marketing/feature note.
   - Fixed Voice Pipeline Architecture selector to Universal Cascade (`pipeline-mode`), preventing any deprecated configuration options.

2. **Client-side JavaScript Updates:**
   - In `saveConfiguration()` and `applyConfiguration()`, removed reads/writes to removed S2S input elements.
   - Cleaned `handlePipelineModeChange()` so that no S2S visibility toggling occurs.

3. **Remaining Active Studio Controls:**
   - STT Providers (Parakeet Local, SenseVoice, Deepgram Flux, Whisper MLX/CUDA/Turbo/Distil/Local)
   - LLM Providers (Antigravity Gemini, Ollama, OpenAI, OpenRouter, LM Studio)
   - TTS Providers (Kokoro-82M Studio Local, Supertonic Local, Edge Neural, Google Gemini TTS, VibeVoice Realtime)
   - Telephony & runtime parameters: Call channel (GSM/WhatsApp), Speculative turns, Conversational reflexes, Auto-answer, CRM/ERP integration, WhatsApp Link, Tools & MCP, and Identity workflow track.

---

## 3. Verification & Gate Results

| Stage | Command | Result | Details |
|---|---|---|---|
| Fast Unit | `./ci/run-stage.sh fast-unit` | PASS | 683 passed, 39 skipped, 1 deselected |
| Quality | `./ci/run-stage.sh quality` | PASS | Ruff, ty (0 errors, 0 warnings), WhatsApp freeze, feature flags, S2S inventory pass |
| Package | `./ci/run-stage.sh package` | PASS | Wheel (`6f919d...`) and SDist (`aac4bf...`) verified |
| Security | `./ci/run-stage.sh security` | PASS | Pip-audit passes with 1 owned torch exception |
| SBOM | `./ci/run-stage.sh licence-sbom` | PASS | Dependency and licensing inventory verified |

---

## 4. Artifacts Produced

- Evidence Bundle: `reports/quality/2026-09-03-m1-06-evidence.json`
- Full Quality Report: `reports/quality/2026-09-03-m1-06-studio-controls-removal.md`
