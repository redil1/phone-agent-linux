# Milestone 0 initial-baseline rollback

The frozen rollback target is `gsm-cascade-2026-09-02`. Its authoritative machine record is
`reports/baselines/2026-09-02-gsm-cascade-baseline.json`; never substitute a mutable tag for the
recorded image ID.

## Runtime rollback

First perform a read-only validation:

```bash
uv run python -m phone_agent_gateway.qualification.initial_baseline_rollback \
  --output artifacts/rollback/m0-plan.json \
  --require-qualified
```

Apply only while the authoritative status endpoint reports `call_state=IDLE`:

```bash
uv run python -m phone_agent_gateway.qualification.initial_baseline_rollback \
  --apply-runtime \
  --output artifacts/rollback/m0-runtime-drill.json \
  --require-qualified
```

The command refuses a partial image ID, a missing or mismatched local image, an active call, a
Compose plan that does not select the exact frozen image, failed health, stale worker
configuration, or provider/model drift. It recreates only `phoneagent`, preserves the mounted
configuration/data/cache volumes and all unrelated services, and uses neither build nor pull. If
the baseline fails to become healthy, it recreates the service on the exact pre-drill image before
returning failure. When the baseline is already active, `--apply-runtime` is intentionally a
verified no-op.

After rollback, all of these must agree with the frozen report:

- container image ID;
- Cascade pipeline mode;
- STT, LLM and TTS provider/model/voice;
- worker readiness and configuration-current state;
- idle call state.

## Android recovery boundary

The initial qualification device has two independently recorded APK anchors:

- installed update: `9fd917778f44efd46d3ac3fd1ac90d3b4773547c0614989af3d3595e12051d55`;
- baked system APK: `51686d7325a31bc1df487c8bf31c9be82f0622d4ea41941ae5f62f10d34ee35e`.

A normal reboot preserves the installed update. Android **Uninstall updates** or a factory reset
falls back to the baked system APK. Reinstalling an update is permitted only when the APK bytes
match the first hash and the signing certificate matches the baked application. A persistent-image
rollback uses `android_service_apk/flash_persistent_gsi.sh` with an untouched, equal-sized rollback
image; its preflight refuses an active Telecom call, the wrong device/build/slot, enabled AVB,
low battery, a partition-size mismatch, or an invalid image. Flashing or uninstalling updates is a
real device mutation and requires explicit operator authorization; a Milestone 0 runtime drill does
not perform either action.

## Evidence interpretation

Unit tests simulate a candidate-to-baseline restoration, active-call refusal, exact-image
selection, worker read-back, and automatic restoration of the previous image after a failed target.
The live M0 drill additionally verifies the exact local image and current qualified worker. This is
the initial single-host recovery contract; atomic Agent Package rollback and production deployment
provenance remain owned by M4 and M17.
