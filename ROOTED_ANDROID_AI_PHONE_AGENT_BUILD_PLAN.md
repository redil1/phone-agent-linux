# Rooted Android AI Phone Agent: End-to-End Build Strategy

## Document purpose

This document defines a practical strategy for building a high-quality, ultra-low-latency AI phone agent without Twilio. The existing SIM and rooted Android phone provide the cellular connection. A Mac that runs continuously provides speech recognition, conversational intelligence, business tools, speech synthesis, storage, and observability.

The target is not merely a demonstration that can answer a call. The target is a full-duplex system that feels conversational, supports reliable barge-in, performs useful business actions, survives unattended operation, and can be measured and improved.

The single most important uncertainty is the Android telephony audio path. Root access makes the design plausible, but it does not guarantee that the phone's vendor Audio HAL exposes a clean cellular downlink or permits digital audio injection into the cellular uplink. This must be proven on the exact phone, ROM, chipset, carrier, and VoLTE configuration before the complete system is built.

## Executive recommendation

Build the system as two cooperating products:

1. **Android cellular gateway**: a privileged, rooted Android application that controls SIM calls and exchanges real-time audio with the Mac.
2. **Mac voice-agent service**: a Pipecat-based streaming pipeline that performs turn detection, STT, LLM/tool execution, TTS, interruption handling, state management, and telemetry.

Use a short technical spike to validate Android audio before investing in the full application. The project receives a **go** only when all four conditions below work during a real cellular/VoLTE call:

- The application can answer and originate a call without human interaction.
- It can capture the remote caller as a clean digital downlink, preferably without the locally injected voice mixed into it.
- It can inject generated PCM audio directly into the cellular uplink without using the physical speaker and microphone.
- It can immediately discard queued output when the caller interrupts.

If call control works but clean audio capture or uplink injection fails, the application is not yet a viable digital gateway. Rooting, privileged installation, SELinux rules, Audio Policy changes, vendor mixer controls, or a device-specific Audio HAL modification may be required.

## Desired outcome and success targets

The system should provide:

- Inbound calls to the existing SIM number.
- Outbound calls from the existing SIM and caller identity.
- Natural, interruptible conversation.
- Tool use such as scheduling, CRM lookup, lead qualification, order status, notifications, and human escalation.
- Per-call transcripts, events, performance metrics, and configurable recording.
- Automatic startup and recovery on both the Android phone and Mac.
- One active call at a time, because one consumer SIM normally supports one cellular voice call.

### Latency service-level objectives

All latency must be measured at percentiles, not with one good demonstration.

| Metric | Initial target | Mature target |
|---|---:|---:|
| Caller end-of-turn to first audible agent audio | P50 <= 650 ms, P95 <= 1,000 ms | P50 300-500 ms, P95 <= 800 ms |
| Caller speech onset to agent audio muted (barge-in) | P95 <= 250 ms | P95 <= 180 ms |
| STT partial transcript delay | P50 <= 250 ms | P50 <= 180 ms |
| LLM time to first token | P50 <= 250 ms | P50 <= 150 ms |
| TTS time to first audio | P50 <= 150 ms | P50 <= 100 ms |
| Unexpected call termination | <1% during pilot | <0.2% |
| Gateway reconnect after local network loss | <=10 seconds | <=5 seconds |

The public cellular network contributes unavoidable latency and codec limitations. The most useful conversational metric starts at the caller's last detected phoneme and ends when the first agent audio sample is injected into the phone uplink. A second metric should measure what the caller actually hears when a controlled test endpoint is available.

## System architecture

```text
                              CONTROL PLANE
                  call state, tools, policy, configuration

  Cellular caller
        |
        | carrier voice / VoLTE
        v
+---------------------------+      private low-latency link      +-----------------------------+
| Rooted Android phone      |<==================================>| Mac agent service           |
|                           |      binary audio + control        |                             |
| Default dialer            |                                    | Gateway session manager     |
| InCallService             |                                    | Pipecat pipeline            |
| Call state machine        |                                    | VAD / turn detection        |
| Privileged AudioBridge    | -- caller audio -----------------> | Streaming STT               |
| Uplink injection buffer   | <------------------ agent audio -- | Streaming LLM + tools       |
| Watchdog / telemetry      |                                    | Streaming TTS               |
+---------------------------+                                    | Context and observability   |
                                                                 +-----------------------------+

                               MEDIA PLANE
             timestamped, sequenced, cancellable full-duplex audio
```

The Android gateway should not contain STT, LLM, TTS, CRM credentials, or business logic. It should remain a small telephony appliance. The Mac should not contain Android-vendor audio hacks. This separation keeps the AI pipeline portable if the telephony transport changes later.

## Rooted Android gateway

### Why root materially changes feasibility

A normal third-party Android application can become the default dialer and manage calls through `InCallService` and `TelecomManager`. It cannot normally capture cellular uplink/downlink audio because `CAPTURE_AUDIO_OUTPUT` is reserved for privileged system components. Root allows the project to explore a privileged installation, permission allowlisting, SELinux changes, Audio Policy configuration, vendor mixer controls, and—if necessary—framework or Audio HAL modifications.

Root alone is not proof that the audio path is available. Android telephony audio is implemented partly by device-specific vendor components. A Magisk-rooted application may still be blocked by signature permissions, SELinux, the vendor HAL, the modem/IMS stack, or missing mixer routes. A stable implementation may require a custom ROM build or a small device-specific native component.

### Android application responsibilities

The gateway application should contain these components:

| Component | Responsibility |
|---|---|
| `GatewayInCallService` | Receive call objects and state changes; answer, reject, disconnect, hold, and report audio routes. |
| `DialerActivity` | Satisfy the Android default-dialer role and provide a minimal emergency/manual UI. |
| `GatewayForegroundService` | Maintain the Mac connection, call session, notification, wake lock, and health reporting. |
| `CallController` | Place outbound calls through `TelecomManager`, select the SIM/phone account, and enforce one-call concurrency. |
| `AudioBridge` | Capture the cellular downlink, inject agent audio into the uplink, expose buffer flush, and report timestamps/xruns. |
| `GatewayProtocolClient` | Send control messages and binary audio frames over a persistent authenticated WebSocket. |
| `GatewayStateMachine` | Make call transitions deterministic and idempotent. |
| `BootReceiver` | Restart the gateway after an intentional device reboot, subject to Android service restrictions. |
| `Watchdog` | Detect stalled audio, dead WebSockets, invalid call states, and excessive buffer growth. |
| `LocalDiagnostics` | Export redacted logs, mixer route information, latency samples, and device/build identifiers. |

### Call-control design

The application should request the default dialer role and implement the complete `InCallService` contract. Use official Telecom APIs for normal call operations instead of automating the stock dialer UI. UI automation through Accessibility or shell input should be a diagnostic fallback, not the production control path.

Required operations:

- Receive `onCallAdded` and register state callbacks.
- Answer an incoming audio call.
- Reject or disconnect a call.
- Place an outgoing `tel:` call using the intended SIM/phone account.
- Detect ringing, dialing, connecting, active, held, disconnected, and failed states.
- Report carrier/IMS state where the device permits it.
- Provide a manual safe mode so a human can recover the phone.
- Refuse emergency-number automation.

The call state machine should be explicit:

```text
OFFLINE -> READY -> RINGING -> ANSWERING -> ACTIVE
READY -> DIALING -> CONNECTING -> ACTIVE
ACTIVE -> ENDING -> READY
any state -> DEGRADED -> RECOVERING -> READY/OFFLINE
```

Every command from the Mac needs a unique command ID. Repeating `answer`, `hangup`, or `dial` after a reconnect must not create duplicate behavior.

### Audio feasibility sequence

Do not begin by building the full UI. First create a diagnostic APK/native harness and test the exact telephony path.

#### Gate A: inspect the device

Collect:

- Manufacturer, model, chipset, Android API level, build fingerprint, ROM, kernel, and root method.
- Available telephony audio input/output devices.
- `/vendor/etc/audio_policy_configuration.xml` and included policy files.
- Audio HAL version and vendor audio service names.
- ALSA mixer controls using `tinymix` where available.
- `dumpsys audio`, `dumpsys telecom`, and relevant `logcat` output before and during a call.
- Whether the call is circuit-switched, VoLTE/IMS, or Wi-Fi calling.

All initial inspection commands should be read-only. Preserve original vendor files before any change.

#### Gate B: capture the caller

Test these paths during a real call:

1. `AudioRecord` with `VOICE_DOWNLINK`.
2. `AudioRecord` with `VOICE_CALL` to determine whether only a mixed stream is available.
3. Selection of `AudioDeviceInfo.TYPE_TELEPHONY` where exposed.
4. Native capture through the vendor/ALSA device if framework capture is blocked.

The preferred result is **remote downlink only**. A mixed uplink-plus-downlink recording causes the agent to transcribe its own synthesized voice, damages VAD, and makes barge-in unreliable. Acoustic echo cancellation can reduce this problem but is inferior to receiving the separate downlink.

Record a test phrase and verify:

- No silence or permission denial.
- No local microphone contamination.
- No injected TTS loopback.
- Stable sample rate and frame cadence.
- No periodic gaps, duplicated audio, or clock drift.

#### Gate C: inject agent audio

Test whether PCM written by the application can reach the remote caller without being played acoustically. Possible implementation levels, from least to most invasive:

1. A vendor-exposed telephony TX audio device or mixer route.
2. An Audio Policy route accessible to a privileged application.
3. A small native service that writes to the telephony uplink path.
4. A device-specific Audio HAL patch or virtual audio source mixed into the modem uplink.

Playing an `AudioTrack` with a voice-call volume category does not by itself prove uplink injection; it may only play through the local earpiece or speaker. The success test must be recorded at the remote end.

The injection implementation must provide:

- `write(frame, generation_id, timestamp)`.
- `flush(generation_id)` that discards queued audio immediately.
- A small bounded jitter buffer.
- Playback position or acknowledgement events.
- Underrun/overrun counters.
- Gain control without clipping.

#### Gate D: validate full duplex and interruption

While agent audio is being injected, the caller speaks over it. Confirm that:

- Caller audio continues to arrive.
- VAD detects the caller rather than the injected voice.
- The Mac cancels the current response.
- Android flushes its output queue.
- Agent audio stops within the barge-in target.

If the device offers only half-duplex telephony audio, it cannot deliver the intended natural conversation quality.

## Android-to-Mac gateway protocol

### Transport selection

Use one long-lived connection per phone, with one logical call session at a time.

Preferred link order:

1. Wired Ethernet/USB network interface when the device supports it.
2. A dedicated local Wi-Fi network with strong signal and no client isolation.
3. `adb reverse` for development only.

Benchmark the actual phone/Mac combination. PCM requires little bandwidth: mono 16-bit audio at 16 kHz is approximately 256 kbit/s per direction before protocol overhead. On a private local link, raw PCM avoids codec delay and makes debugging easier than Opus.

### Audio framing

Recommended initial format:

- Mono signed 16-bit little-endian PCM.
- Native telephony sample rate when known; otherwise 16 kHz inside the AI pipeline.
- 20 ms gateway frames.
- Monotonic capture timestamps.
- Sequence numbers and explicit direction.
- A `call_id` and `generation_id` on every frame.

If the phone exposes 8 kHz cellular PCM, keep it at 8 kHz across the gateway and resample once, immediately before an STT service that benefits from 16 kHz. Do not repeatedly convert 8 -> 16 -> 24 -> 8 kHz. TTS should ultimately be converted once to the native injection format.

Deepgram Flux recommends roughly 80 ms chunks for its STT input. The gateway can still transport 20 ms frames; the Mac should aggregate four consecutive frames before sending them to Flux. This preserves responsive local VAD and cancellation while satisfying the STT model's preferred chunking.

### Control messages

Use versioned JSON or Protobuf control messages alongside binary audio:

```json
{
  "v": 1,
  "type": "call.state",
  "phone_id": "gateway-01",
  "call_id": "local-call-id",
  "seq": 183,
  "state": "active",
  "monotonic_ms": 8723341
}
```

Required message families:

- `gateway.hello`, `gateway.ready`, `gateway.health`.
- `call.incoming`, `call.dial`, `call.answer`, `call.hangup`, `call.state`.
- `audio.start`, `audio.format`, `audio.ack`, `audio.flush`, `audio.stop`.
- `dtmf.received` where the platform exposes it.
- `error`, `warning`, and `metrics.batch`.

### Buffering rules

Excess buffering creates latency and makes interruptions sound broken.

- Gateway input queue: target 20-60 ms, maximum 120 ms.
- Gateway output queue: target 40-80 ms, maximum 120 ms.
- Drop stale audio rather than playing it late.
- Never allow an unbounded queue.
- On every new `generation_id`, reject late frames from older generations.
- On `audio.flush`, discard application, native, and HAL-side buffers as far downstream as technically possible.

## Mac voice-agent service

### Recommended logical pipeline

```text
Android input
  -> frame validation and clock metrics
  -> caller-only audio processor
  -> local VAD for immediate speech-start/barge-in
  -> streaming STT + conversational turn detector
  -> context/state aggregator
  -> streaming LLM and tool router
  -> speakable-clause aggregator
  -> streaming TTS
  -> generation/cancellation guard
  -> Android output
```

Pipecat is suitable because it models these operations as streaming frames and propagates interruption frames through processors. Its interruption lifecycle can cancel an in-flight LLM response, clear TTS queues, and flush transport output. A custom Android serializer/transport should translate Pipecat audio/control frames into the gateway protocol.

### Per-call isolation

Create one pipeline worker per active call. Even though the SIM supports only one call, isolation prevents stale state from leaking between consecutive calls.

Each call owns:

- STT and TTS WebSocket sessions.
- An LLM conversation context.
- A unique cancellation scope.
- Tool execution IDs and idempotency keys.
- Audio generation IDs.
- Metrics and structured logs.

Persistent provider connections can be established as soon as the call enters `ANSWERING` or `CONNECTING`, before the first caller utterance. This hides TLS/WebSocket setup latency.

## Turn detection and barge-in

Turn-taking quality has a larger effect on perceived intelligence than raw model size.

### Speech start

Run a lightweight local VAD on Mac audio frames. Speech start should immediately create an interruption event when the agent is speaking. Do not wait for a complete STT transcript before stopping output.

The interruption must fan out concurrently:

1. Cancel the current LLM stream.
2. Cancel or ignore the current TTS generation.
3. Clear Pipecat output frames.
4. Send `audio.flush` to Android.
5. Increment `generation_id`, causing late audio to be rejected.
6. Mark the assistant response as interrupted in conversation state.

The context should reflect only what the caller could reasonably have heard. If exact playback acknowledgements are available, retain the spoken prefix and label the message as interrupted. Never store an entire generated answer as though it was played.

### Speech end

Recommended starting strategy:

- Use Deepgram Flux as the conversational STT and end-of-turn detector.
- Start with normal `EndOfTurn` events until quality is stable.
- Add `EagerEndOfTurn` speculative LLM generation afterward.
- On `TurnResumed`, cancel the speculative response immediately.
- Do not release speculative TTS audio to the caller until the turn is confirmed.

An initial low-latency Flux configuration can test an eager threshold around 0.4 and confirmed threshold around 0.7, but thresholds must be learned from real calls, languages, accents, noise, and business tasks. Lower thresholds improve response time while increasing false endpoints and wasted LLM calls.

Track:

- Eager events confirmed versus resumed.
- False interruptions per call.
- Caller words clipped by early endpointing.
- Silence between caller turn end and agent audio.
- Agent responses cancelled before any audio played.

## STT strategy

Start with Deepgram Flux because it combines streaming transcription with conversation-oriented turn events. Use the English or multilingual model appropriate to the actual callers; language hints should be narrowly configured when multilingual mode is used.

Guidelines:

- Keep its WebSocket open for the entire call.
- Send correctly timestamped audio continuously.
- Aggregate gateway frames to the provider's preferred chunk duration.
- Maintain keepalives according to the provider protocol.
- Configure important names, product terms, and domain vocabulary.
- Preserve raw interim/final events in diagnostic logs with sensitive-data controls.
- Never block the audio receive loop on database or tool work.

Build a provider interface so Nova-3 or another STT can be benchmarked against Flux. The decision should use real telephone recordings and evaluate word error rate, entity accuracy, endpoint latency, and false-turn behavior—not marketing latency alone.

## LLM and agent intelligence strategy

The fastest system is not the one with the smallest model everywhere. It is the system that avoids unnecessary model work.

### Separate deterministic control from language generation

Use a deterministic application state machine for call policy and a streaming LLM for understanding and expression.

The LLM may decide:

- Caller intent and conversational response.
- Which approved tool should be invoked.
- Which missing field should be requested.
- Whether clarification is required.

The application—not the model—must enforce:

- Allowed tools and argument schemas.
- Authentication and authorization.
- Business invariants.
- Retry limits and timeouts.
- Call transfer/hangup policy.
- Idempotency for bookings, payments, and messages.

### Reduce LLM latency

- Use streaming output.
- Keep the system prompt compact and structured.
- Store structured call state rather than replaying an unlimited transcript.
- Summarize old conversation turns asynchronously.
- Keep tool schemas small and expose only tools valid in the current state.
- Prefer short spoken responses, usually one or two sentences before yielding.
- Route ordinary turns to a fast model and escalate only genuinely complex turns.
- Do not use a reasoning model for greetings, confirmations, or simple field collection.
- Preload caller/account context before answering when caller identity is available.
- Cache stable knowledge and tool metadata locally.

### Tool execution

Tools should be asynchronous and observable. Give every operation a deadline and idempotency key.

For a tool expected to take longer than approximately 500 ms:

1. Produce a brief natural acknowledgement only when useful.
2. Execute the operation asynchronously.
3. Stream the final result when ready.
4. Allow caller interruption while waiting.
5. Cancel only safe-to-cancel operations; durable operations should complete and report their eventual result.

Prefetching is powerful. At call start, retrieve the caller profile, open appointments/orders, business hours, and relevant policies concurrently. Do not perform broad RAG retrieval on every conversational turn.

## TTS strategy

Use a streaming TTS service with a persistent WebSocket. Cartesia Sonic is a strong initial candidate because its WebSocket supports streaming inputs, contexts, direct telephony formats, and low time to first audio. Benchmark at least one alternative using identical text and telephone playback.

### High-quality low-latency synthesis rules

- Open the TTS WebSocket during call setup, not after the first LLM sentence.
- Stream speakable clauses rather than individual tokens.
- Start on a stable short clause or punctuation boundary; do not wait for the entire response.
- Preserve spaces and punctuation across continuation messages for prosody.
- Use one TTS context/generation ID per assistant turn.
- Prefer raw PCM or raw 8 kHz mu-law only when it matches the Android injection path.
- Avoid WAV/container headers in real-time raw audio.
- Keep no more than roughly 80-120 ms queued after the current playback point.
- On interruption, cancel pending TTS work, reject remaining chunks by generation ID, and flush Android.

Cartesia documentation notes that cancelling a context may not stop a request that has already begun generating. Therefore, client-side generation filtering and Android buffer flushing are mandatory even when a provider cancel message is sent.

Telephone audio is usually the quality ceiling. A 44.1 kHz TTS output does not create a 44.1 kHz cellular call. Choose the pipeline format that minimizes conversion and preserves intelligibility, pronunciation, and stable loudness within the carrier codec.

## Practical latency budget

The stages overlap when streaming and eager end-of-turn are enabled.

| Stage | Target contribution | Optimization |
|---|---:|---|
| Android capture and framing | 20-50 ms | Native digital route, 20 ms frames, no file encoder. |
| Android-to-Mac transport | 5-25 ms | Private local link, persistent socket, TCP_NODELAY where appropriate. |
| Confirmed/eager turn detection | 100-300 ms | Flux tuning; speculative generation overlaps detection. |
| LLM first usable text | 80-250 ms | Small context, fast model, streaming, preloaded state. |
| Speakable-clause aggregation | 30-120 ms | Short voice-oriented responses and early stable clauses. |
| TTS first audio | 40-150 ms | Warm WebSocket, fast streaming model. |
| Mac-to-Android and injection | 20-80 ms | Small bounded queue and native format. |

A simple sum overstates optimized latency because eager STT/turn events, LLM generation, and preparation can overlap. The mature goal of 300-500 ms P50 is ambitious but plausible only after the digital Android path is proven and all connections stay warm.

Do not sacrifice stability merely to win P50. Track P95/P99, interruptions, false endpoints, and failed calls. A consistent 600 ms response is better than alternating between 300 ms and two seconds.

## Audio quality engineering

High quality requires more than choosing a good TTS voice.

- Keep the caller and agent audio paths separate.
- Avoid acoustic speakerphone coupling.
- Avoid multiple sample-rate conversions.
- Normalize TTS gain once and prevent clipping.
- Measure packet cadence, drift, jitter, underruns, and overruns.
- Apply noise suppression only when telephone noise requires it.
- Avoid stacking Android, STT-provider, and pipeline AGC/noise suppression without testing; double processing can damage speech.
- Preserve brief natural pauses in TTS while limiting long dead air.
- Build pronunciation dictionaries for names, products, addresses, numbers, and local languages.
- Test with weak signal, background noise, speakerphone callers, Bluetooth callers, accents, and code-switching.

Use a fixed corpus of real-world test calls so changes to VAD, STT, prompts, models, and TTS can be compared objectively.

## Reliability and unattended operation

### Android

- Run the gateway as a foreground service with a persistent notification.
- Acquire wake locks only as needed during calls and reconnects.
- Exempt the gateway from vendor battery optimization.
- Start after boot and verify the default-dialer role remains assigned.
- Reconnect with capped exponential backoff and jitter.
- Store only minimal local state; recover the active call from Telecom after process restart.
- Monitor temperature, charging state, free storage, network state, modem service, and audio xruns.
- Schedule controlled health checks, not disruptive automatic reboots during active calls.
- Use a reliable charger and consider battery-preservation settings for continuous power.

### Mac

- Run the agent supervisor through `launchd`.
- Separate the gateway/session service from optional dashboards and batch jobs.
- Add health endpoints for gateway connection, provider connectivity, and readiness.
- Use structured JSON logs with call IDs and generation IDs.
- Rotate logs and recordings.
- Keep a local queue for post-call events when external services are unavailable.
- Gracefully drain an active call during application updates.

### Failure behavior

Define behavior before implementation:

| Failure | Required behavior |
|---|---|
| Mac connection lost before answer | Do not answer, or route to a configured manual/voicemail policy. |
| Mac connection lost during call | Play a locally cached failure message if digital injection remains available, then end or transfer safely. |
| STT unavailable | Retry only within a short deadline; do not leave indefinite silence. |
| LLM unavailable | Use a bounded retry or a smaller fallback model/provider. |
| TTS unavailable | Use a second provider or cached critical phrases. |
| Android audio capture stalls | Stop the session, report the fault, and avoid fabricating caller input. |
| Android output stalls | Flush, reset the audio route once, then fail safely. |
| Tool timeout | Tell the caller the action did not complete; never claim success. |

## Security, privacy, and compliance

A rooted phone has a larger attack surface, so it should be treated as an appliance.

- Use an authenticated encrypted connection between phone and Mac, ideally mutual TLS or a pinned device key.
- Do not expose ADB over a public or untrusted network.
- Do not store STT/LLM/TTS provider secrets on Android.
- Restrict gateway commands to an allowlist and bind them to the current call ID.
- Reject replayed or out-of-order control commands.
- Encrypt sensitive records at rest and define retention periods.
- Redact secrets and personal data from logs.
- Provide recording/AI disclosure and consent where applicable.
- Review local laws, carrier terms, outbound-calling rules, do-not-call requirements, and emergency-call handling before production use.
- Never automate emergency calls through the AI agent.

## Observability

Every call should produce a structured timeline:

```text
call_ringing
call_answer_requested
call_active
stt_connected
tts_connected
user_speech_started
stt_eager_eot
stt_end_of_turn
llm_first_token
tts_first_audio
android_first_audio_written
android_playback_ack
user_interrupted
audio_flush_ack
call_ended
```

Measure:

- End-of-turn to first audio at P50/P90/P95/P99.
- Speech-start to mute for interruptions.
- Provider connection and reconnection time.
- STT confidence/entity accuracy and false endpoints.
- LLM TTFT, prompt tokens, output tokens, and cancellations.
- TTS TTFA, characters, audio duration, and discarded audio.
- Android queue depth, xruns, dropped/stale frames, CPU, memory, and temperature.
- Tool success, timeout, and duplicate-prevention rates.
- Cost per successful call and per minute.

Pipecat provides performance and usage metrics plus observers for user-to-bot latency and turn tracking. Add gateway-specific metrics because Pipecat cannot see audio still buffered inside Android or the vendor HAL.

## Testing strategy

### Unit and protocol tests

- State-machine transition tests.
- Idempotent repeated commands.
- Audio frame serialization and sequence gaps.
- Generation cancellation and stale-frame rejection.
- Ring buffer bounds and overflow behavior.
- Tool schemas, authorization, and idempotency.

### Audio tests

- Known waveform sent through Mac -> Android -> remote recording.
- Remote waveform -> Android -> Mac capture.
- Frequency response, clipping, signal-to-noise ratio, gaps, and clock drift.
- Thirty-minute continuous call.
- Simultaneous speech and TTS.
- Repeated flush during a long generated response.

### Conversation tests

- Short question/answer turns.
- Long caller pauses that should not end the turn.
- Caller self-correction after an eager end event.
- Barge-in at the start, middle, and end of agent speech.
- Background television and cross-talk.
- Numbers, names, addresses, spelling, dates, and confirmation.
- Tool success, slow tool, timeout, duplicate result, and partial failure.
- Language and accent cases that match actual callers.

Pipecat scenario/evaluation tooling can anchor a caller interruption to an agent event, which is useful for reproducible barge-in regression tests.

### Reliability tests

- 100 consecutive inbound and outbound calls.
- Wi-Fi/USB link interruption during idle and active calls.
- Android app process kill.
- Mac service restart.
- Phone and Mac reboot.
- STT, LLM, TTS, and business API failure injection.
- Low storage, high CPU load, thermal stress, and weak carrier signal.

## Phased implementation plan

### Phase 0: device inventory and acceptance criteria (0.5-1 day)

- Record exact phone/ROM/root/carrier/VoLTE details.
- Define supported languages and primary business use case.
- Establish baseline call audio quality using a normal human call.
- Create the latency and reliability test sheet.

**Exit condition:** test environment and success criteria are documented.

### Phase 1: Android audio feasibility spike (2-5 days)

- Inspect Audio Policy, HAL, devices, mixer controls, Telecom, and IMS state.
- Build the privileged capture/injection diagnostic.
- Test downlink-only capture and digital uplink injection.
- Implement and measure output flush.

**Exit condition:** a remote caller can hear injected digital audio while the Mac receives caller-only digital audio during simultaneous speech.

**Stop condition:** no viable separate downlink or uplink route can be found without a major ROM/HAL project that exceeds the intended scope.

### Phase 2: Android call-control gateway (3-6 days)

- Implement default-dialer role and `InCallService`.
- Implement inbound answer, outbound dial, hangup, call states, and safe manual UI.
- Add foreground operation, boot recovery, device identity, and health reporting.
- Create a deterministic call state machine.

**Exit condition:** 50 consecutive test calls complete without manual UI intervention.

### Phase 3: real-time gateway protocol (3-5 days)

- Implement authenticated persistent WebSocket.
- Add binary PCM frames and versioned controls.
- Add timestamps, sequence numbers, generation IDs, bounded queues, and flush acknowledgements.
- Characterize latency and jitter over the selected local link.

**Exit condition:** stable full-duplex audio for at least 30 minutes with measured queue depth and no unbounded drift.

### Phase 4: baseline Mac AI pipeline (3-6 days)

- Create custom Pipecat Android transport/serializer.
- Integrate streaming STT, one fast streaming LLM, and streaming TTS.
- Keep provider connections warm per call.
- Add a concise voice-oriented system prompt and basic context handling.

**Exit condition:** natural multi-turn inbound and outbound conversations work end to end.

### Phase 5: turn-taking and hyper-low latency (4-8 days)

- Add local VAD interruption.
- Propagate cancellation through LLM, TTS, Pipecat, gateway, and HAL buffers.
- Add confirmed Flux end-of-turn, then eager speculative generation.
- Tune clause aggregation, queue limits, and provider/model selection.
- Establish P50/P95 latency dashboards.

**Exit condition:** barge-in and response latency meet initial targets across the fixed test corpus.

### Phase 6: powerful agent behavior (1-2 weeks)

- Add business state machine and approved tools.
- Add caller-context prefetch and targeted knowledge retrieval.
- Add validation, idempotency, tool deadlines, and honest failure responses.
- Add escalation/transfer policy where the cellular setup supports it.
- Add multilingual behavior if required and verified.

**Exit condition:** the agent completes the chosen business workflow accurately, not merely conversationally.

### Phase 7: production hardening (1-2 weeks)

- Add watchdogs, provider fallbacks, cached critical phrases, log rotation, retention, and alerts.
- Complete failure injection and 100-call soak tests.
- Add consent/compliance behavior.
- Run a limited pilot, then tune from real metrics.

**Exit condition:** reliability, latency, quality, and compliance targets are met for the pilot scope.

An experienced solo engineer should expect approximately four to eight weeks for a robust first production pilot, with the Android audio feasibility spike being the largest schedule uncertainty.

## Suggested repository structure

```text
PhoneAgent/
  android-gateway/
    app/
    native-audio/
    device-overlays/
    diagnostics/
  mac-agent/
    src/
      gateway/
      pipeline/
      turns/
      tools/
      state/
      telemetry/
    tests/
  protocol/
    schemas/
    test-vectors/
  evals/
    audio-corpus/
    call-scenarios/
    latency/
  ops/
    launchd/
    dashboards/
    runbooks/
  docs/
    device-feasibility.md
    architecture.md
    incident-runbook.md
```

## First implementation backlog

1. Collect exact phone and software inventory.
2. Confirm root shell access and current default-dialer behavior.
3. Capture `dumpsys`/Audio Policy/mixer inventory during an idle state and real call.
4. Build a privileged downlink capture test.
5. Build an uplink injection and remote-recording test.
6. Prove output queue flush and simultaneous speech.
7. Select the gateway audio format based on the proven native route.
8. Define protocol schemas and generation semantics.
9. Build the minimal Android call controller and Mac echo server.
10. Integrate the baseline Pipecat/Flux/LLM/TTS pipeline.
11. Add metrics before optimizing latency.
12. Add interruption, then speculative eager end-of-turn.
13. Add one business workflow and test it end to end.
14. Harden only after the measured prototype meets the audio feasibility gate.

## Final decision rule

The rooted-phone design is the best no-new-hardware path and may deliver excellent digital quality. Its feasibility must be decided empirically on the exact device.

Proceed to the complete product only if the Android spike proves:

- Caller-only digital downlink capture.
- Direct digital uplink injection.
- Full duplex during VoLTE calls.
- A flushable output path with acceptable interruption latency.
- Stable unattended behavior.

Once those conditions pass, the remaining architecture—streaming STT, LLM, TTS, tools, metrics, and deployment—is well understood and can be engineered incrementally.

## Primary technical references

- [Android `InCallService`](https://developer.android.com/reference/android/telecom/InCallService)
- [Android `TelecomManager`](https://developer.android.com/reference/android/telecom/TelecomManager)
- [Android `MediaRecorder.AudioSource`](https://developer.android.com/reference/android/media/MediaRecorder.AudioSource)
- [Android audio-input sharing and privileged voice-call capture](https://developer.android.com/media/platform/sharing-audio-input)
- [Android `CAPTURE_AUDIO_OUTPUT` permission](https://developer.android.com/reference/android/Manifest.permission#CAPTURE_AUDIO_OUTPUT)
- [Pipecat interruptions](https://docs.pipecat.ai/pipecat/fundamentals/interruptions)
- [Pipecat metrics](https://docs.pipecat.ai/pipecat/fundamentals/metrics)
- [Pipecat user-turn strategies](https://docs.pipecat.ai/api-reference/server/utilities/turn-management/user-turn-strategies)
- [Deepgram Flux quickstart and audio recommendations](https://developers.deepgram.com/docs/flux/quickstart)
- [Deepgram Flux end-of-turn configuration](https://developers.deepgram.com/docs/flux/configuration)
- [Deepgram eager end-of-turn optimization](https://developers.deepgram.com/docs/flux/voice-agent-eager-eot)
- [Cartesia WebSocket TTS](https://docs.cartesia.ai/api-reference/tts/websocket)
- [Cartesia WebSocket contexts and streaming inputs](https://docs.cartesia.ai/use-the-api/tts-websocket/contexts)
- [Cartesia Twilio integration demonstrating raw 8 kHz mu-law output](https://docs.cartesia.ai/integrations/twilio)

