# M1-02 Cascade playout-ACK investigation

Date: 2026-09-03  
State: implementation and non-device evidence pass; handset qualification pending

## Result

The silent-phone failure is pinned to the version-1 remote-link transport, not STT, LLM, TTS,
Pipecat audio generation, Android `AudioTrack`, or the GSM routing policy. Version 1 multiplexes
capture, playout/ACK, control, and heartbeat traffic over one WAN TCP connection. A blocked capture
write can therefore hold the shared writer and prevent locally generated playout ACKs, control
responses, and PONGs from reaching Linux. The Linux sender exhausts its 30-frame credit windows,
waits six seconds, then correctly fails delivery and disconnects the link.

The permanent protocol fix is remote-link v2: one authenticated coordinator plus a separately
authenticated TCP tunnel for each logical stream. A fresh random 32-byte OPEN challenge binds every
tunnel to its coordinator request. Capture congestion can no longer block playout ACK, control, or
heartbeat traffic. Linux remains compatible with the installed v1 APK, and the new APK explicitly
falls back to v1 when it encounters an old relay.

## Live observations

Two calls on candidate image `sha256:d9bc5d44b5b4fb975c88aeaedd856c0c473959a01948ca4d8057797fccd6b801`
reproduced the same failure:

| Observation | Call A | Call B |
| --- | ---: | ---: |
| Linux output frames accepted before stall | 57 | 60 |
| Linux output frames dropped after credit exhaustion | 442 | 439 |
| ACK-stall deadline | 6 s | 6 s |
| Android evidence | local AudioTrack frames and ACK sends advanced | local AudioTrack frames and ACK sends advanced |
| Control/link behavior | timed out with ACK traffic | timed out with ACK traffic |

The paired Android counters prove TTS PCM reached the phone and `AudioTrack.write` completed. They
do not prove the ACK crossed the WAN: v1 counted an ACK immediately after writing it into the local
8767 socket, before the single remote carrier transported it. Simultaneous control and heartbeat
delays locate the failure at the shared carrier rather than the audio renderer.

An exact M0 rollback A/B call on
`sha256:082c9bfcfd3b87cfdd02ab53c3dd1ffca61d8a07fe1cb433106c93753e1c26fb`
then delivered 1,046 output frames with zero output drops. Its last sent/rendered sequences were
1,048/1,049 and every recorded response reached `playback_status=completed`. This proves the phone,
GSM audio policy, and base playout path remained functional. Production was left on this verified
M0 digest.

No additional call was placed while preparing this report. All live observations use the one
previously authorized destination, represented only by the baseline's redacted identifier.

## Implemented correction

- `ai_bridge/remote_link.py`: v2 coordinator, per-stream carrier authentication, challenge binding,
  stream lifecycle cleanup, v1 compatibility, and negotiated protocol telemetry.
- `android_service_apk/src/com/phoneagent/gateway/RemoteLinkService.java`: v2 independent stream
  sockets, v1 fallback, idle-stream lifetime ownership, synchronized attach/close behavior, and
  negotiated-version read-back.
- `android_service_apk/src/com/phoneagent/gateway/DigitalAudioBridge.java`: call-state recheck after
  `AudioTrack.write`, preventing normal call teardown from being reported as a playout fault.
- Both Android health protocols now report immutable APK source provenance, supported remote-link
  version, and negotiated remote-link version.
- The installer now fails closed on zero/multiple/unauthorized devices, uses an explicit Android 34
  toolchain, backs up the previous APK and privilege allowlist, verifies installed bytes and
  privileged grants, validates runtime provenance, and writes a serial-redacted rollback receipt.
- Formal device qualification requires the exact APK source digest and both supported and
  negotiated remote-link version 2; legacy `phag_v1_hmac_sha256` media framing alone cannot pass.
- Telephony HTTP tests use an ephemeral loopback server, so an accidental pytest selector cannot
  issue DTMF or hangup commands to a real phone.

## Verification

- Cascade characterization: 10/10 shared behaviors mapped to 31 executable nodes; matrix SHA-256
  `95115a2c09109f5837e847ca993c8556df8e06c251391091eddb89f7e9cf6b9f`.
- Remote-link Python v1/v2 suite: 27 passed. The v2 congestion regression blocks capture drainage
  and proves ACK/control progress in less than 0.5 seconds. Wrong challenges are rejected.
- Android executable protocol checks: `ProtocolCodec Java/Python golden vector passed`,
  `remote-link-interop-ok`, and `remote-link-v2-isolation-ok`. The last test proves distinct data
  sockets, exact challenge binding, capture/ACK isolation, and old-relay fallback.
- Repository gates: 927 passed, 40 skipped, one real-device test deselected; Ruff and strict Pyright
  pass. Fast-unit reports 772 passed/39 skipped/one deselected; integration reports 227 passed.
- Cascade eval: 158 passed and the deterministic Cascade performance contract passes all six
  thresholds.
- Package, container contract, security, SBOM/licence, and Android protocol stages pass. The known
  NLTK advisory remains covered by the existing bounded policy exception.

## Release artifacts

| Artifact | SHA-256 / identity |
| --- | --- |
| APK | `7e059cc98a093176014b3ffb097f11439a66ad4901ca0639751e9706fa8e2d3b` |
| APK embedded Android source | `d23bd6d7165450b70a3f7bfeb9457bab2156feb52fccbcbb5f73c49e16dce307` |
| APK signer certificate | `ffcdc9ab3dbbafb34a95b2d8b87545efc3b2d78f01c9f17dfc998a85b139c1aa` |
| Python wheel | `751312f0a6c1f688bb94fa08fe303f7f35a34bc0bb585734950126f6957dd64d` |
| Normalized sdist | `663242bc79c8ba4f2eb49f158f669906cd6f7f695f8fb0ae847c3e85cdec8fd0` |
| Candidate container | `sha256:aac9aaac00c81a25d3dbd8551439170181977f4597cd22920190a0e34b0b6861` |
| Preserved production container | `sha256:082c9bfcfd3b87cfdd02ab53c3dd1ffca61d8a07fe1cb433106c93753e1c26fb` |

## Remaining acceptance gate

All known Mac routes (`100.73.112.70`, `105.190.173.187`, and `196.119.87.246`, ports 22/2223)
timed out or were unroutable at the end of this cycle. The current authenticated Android gateway
does not expose a safe APK-update command, so bypassing ADB would be an unqualified deployment
mechanism.

When Mac/ADB access returns: back up and hash the installed APK, install the artifact above, verify
updated privileged-system-app and reboot persistence, require health read-back of source digest
`d23bd6...307` and negotiated protocol `2`, deploy the candidate by exact digest, then place one
explicitly authorized qualification call. Acceptance requires completed opening and response
playout ACKs, zero output drops/sequence gaps/flush failures/starvation/concealment/underruns, and
successful rollback evidence. Until then M1-02 remains active and M1-03 deletion does not start.
