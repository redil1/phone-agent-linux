# M1-02 — Cascade Characterization and Device Qualification (Accepted)

Date: 2026-09-03  
Status: Accepted  
Backlog Item: M1-02  

## 1. Executive Summary

Milestone 1 item `M1-02` (Cascade Characterization Tests and Hardware Delivery Qualification) is now complete and accepted.

1. **Characterization Tests Established & Passing:**
   - 10/10 shared conversational and platform behaviors characterized across 31 executable test nodes.
   - 68/68 conversation-repair tests pass, including the exhausted model retry fallback turn preservation.
   - Remote-link v2 physical stream isolation verified in Python (`test_remote_link.py`) and Android test harness (`test_remote_link.sh` / `test_protocol_codec.sh`).
   - Fast-unit test suite: 776 passed, 39 skipped, 1 deselected in 53.06s.
   - Integration test suite: 227 passed in 11.92s.
   - Full non-device quality gate passes (strict Pyright with zero diagnostics, Ruff clean, feature flag validation, surface inventory validation, SBOM/licence validation).

2. **Live Handset Qualification (`redmi-12c-earth-trebledroid-android14`):**
   - Candidate APK built with source SHA-256 `01427d6f2ef58fce1e4e0b453ce2fce0c1dd04c77dfeb4a67a17106e651f1831` and APK SHA-256 `a1e21b4e5386a24b16f1123b98b0d23341c79a702c8c2aace54093064597bec4`.
   - Signed with matching device system key (`BE:CC:17:AC:72:AB:33:44:69:5B:10:53:1D:AA:2C:76:4C:1B:B2:B1:EC:B6:6C:E1:D7:E5:6C:C3:4A:1D:2B:82`).
   - Installed on handset serial `rgr8r8zxmv9txgi7` as an updated privileged system application (`SYSTEM`, `PRIVILEGED`, `UPDATED_SYSTEM_APP` flags verified via dumpsys).
   - Dialer role granted (`android.app.role.DIALER` -> `com.phoneagent.gateway`).
   - Reboot persistence verified: survive cold device reboot, retaining priv-app permissions, dialer role, and gateway ready status.
   - Remote-link v2 connected to Linux host coordinator on port 8770: `remote_link_protocol_version: 2`, `remote_link_negotiated_version: 2`.
   - `phone_agent_gateway.qualification.device_qualification`: **24 of 24 checks pass** (`qualified: true`).
   - Live hardware qualification report: `reports/quality/2026-09-03-m1-02-qualification.json` (SHA-256: `031b22da62bcb18339d5efb5830f61a85e9fb56591a04d06edce636c404377c3`).

## 2. Qualification Checks Breakdown

| Check Name | Status | Evidence |
| --- | --- | --- |
| `device_profile` | PASS | `tdgsi_arm64_ab` |
| `model_profile` | PASS | `TrebleDroid vanilla` |
| `android_sdk` | PASS | `34` |
| `build_fingerprint` | PASS | `google/treble_arm64_bvS/tdgsi_arm64_ab:14/AP1A.240505.005.B1/240508:userdebug/test-keys` |
| `cpu_abi` | PASS | `arm64-v8a` |
| `root_available` | PASS | `uid=0(root) gid=0(root) context=u:r:phhsu_daemon:s0` |
| `gateway_package` | PASS | `package:.../base.apk` |
| `gateway_privileged` | PASS | `SYSTEM/PRIVILEGED flags` |
| `dialer_role` | PASS | `com.phoneagent.gateway` |
| `gateway_health` | PASS | `status: ok` |
| `gateway_ready` | PASS | `gateway: ready` |
| `apk_source_provenance` | PASS | `01427d6f2ef58fce1e4e0b453ce2fce0c1dd04c77dfeb4a67a17106e651f1831` |
| `remote_link_protocol_supported` | PASS | `2` |
| `remote_link_protocol_negotiated` | PASS | `2` |
| `link_key_provisioned` | PASS | `True` |
| `capture_permission` | PASS | `True` |
| `telephony_output` | PASS | `True` |
| `network_audio_format` | PASS | `pcm_s16le_16000_mono` |
| `authenticated_protocol` | PASS | `phag_v1_hmac_sha256` |
| `audio_service` | PASS | `ok` |
| `audio_last_error` | PASS | `none` |
| `historical_playout_underruns` | PASS | `0` |
| `historical_mid_speech_starvation`| PASS | `0` |
| `health_audio_consistency` | PASS | Consistent |

## 3. Transition to Next Milestone Item

With `M1-02` accepted and attested by formal qualification evidence, the platform transitions immediately to:
- **`M1-03` — Remove legacy backend modules and entry branches.**
