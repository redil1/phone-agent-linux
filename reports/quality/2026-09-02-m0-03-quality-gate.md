# M0-03 Development Quality Gate — 2026-09-02

Status: passed for the maintained non-device test and Ruff scopes.

## Environment

| Component | Value |
| --- | --- |
| Test image | `sha256:082c9bfcfd3b87cfdd02ab53c3dd1ffca61d8a07fe1cb433106c93753e1c26fb` |
| Python | 3.11.15 |
| Pytest | 8.4.2 |
| pytest-asyncio | 1.2.0, strict mode |
| Ruff | 0.12.12 |
| Test source | Disposable writable clone of the current new-project working tree |

## Non-termination root cause

The original run completed 122 test calls and then stopped advancing during teardown of
`test_web_api_does_not_report_qr_ready_session_as_connected`. The test body took 50 ms and passed,
but `pytest-asyncio` blocked in `asyncio.Runner.close()` while awaiting
`loop.shutdown_default_executor()`.

A live `py-spy` dump identified the unbounded default-executor job:

```text
PhoneAgentWebServer._on_startup
  -> _prewarm_gpu_models_task
    -> prewarm_gpu_resident_models
      -> asyncio.to_thread(prewarm_sensevoice)
        -> FunASR / ModelScope snapshot_download
          -> four blocking HTTPS download threads
```

Cancelling the asyncio task could not cancel its running thread. Event-loop teardown therefore
waited indefinitely for a model download that had no bounded completion. The work was also in the
Studio process, while call speech models live in a separate voice-host process, so its CUDA state
could not warm the process that serves a call.

## Fix and regression

- `PhoneAgentWebServer._on_startup` no longer starts process-local speech-model prewarm when
  `PHONE_AGENT_WARM_VOICE_HOST=0`. It reports `deferred` ownership by the per-call voice host.
- `test_disabled_warm_voice_host_does_not_prewarm_models_in_studio` failed before the fix and passed
  afterward.
- The previously hanging OpenWA test then completed and exited in 0.39 seconds under a 30-second
  watchdog.

## Final test evidence

```text
837 passed, 40 skipped, 4 deselected, 3 warnings in 56.68s
```

This is 877 selected non-device tests, including the three new M0 regressions. The process exited
with code 0; a printed Pytest summary alone was not treated as termination proof.

The three warnings are upstream deprecations for Python `audioop`, Pipecat's deprecated
`SpeechTimeoutUserTurnStopStrategy.reset`, and `AudioContextTTSService`. They are carried forward as
dependency-migration work and did not fail the current gate.

## Ruff evidence

Initial configured scan: 472 findings.

- 342 were `E501` source-width findings dominated by prompts, schemas, and embedded JavaScript.
  `E501` is explicitly ignored while all other selected `E`, `F`, `I`, `UP`, `B`, `ASYNC`, and
  `RUF` rules remain enabled.
- `s2s_research` is explicitly quarantined because the forbidden S2S pipeline is deleted in
  Milestone 1 rather than modernized.
- Two byte-frozen WhatsApp compatibility files are excluded from Ruff and remain protected by
  `tests/production/test_frozen_whatsapp.py` plus `release/frozen-whatsapp.sha256`.
- Ruff safe fixes resolved 91 findings; 14 Python 3.11 `isinstance` updates and 14 semantic findings
  were resolved explicitly, including blocking file I/O in async product-research endpoints and
  missing exception chaining.

Final configured scan:

```text
All checks passed!
```

No device integration test, real call, production deployment, APK installation, or container restart
was performed for M0-03.
