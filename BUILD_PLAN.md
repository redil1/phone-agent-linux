# PhoneAgent Full Cellular AI Gateway — Detailed Build Plan

## 1. Mission

Turn a rooted Redmi 12C (`earth`, MediaTek MT6768) into a dedicated cellular
appliance that a Mac-hosted AI can operate end to end.

The completed system must let the AI:

- observe incoming and outgoing cellular calls;
- read caller identity when Android exposes it;
- dial, answer, reject, hang up, and send DTMF;
- receive the remote caller as clean digital PCM;
- inject synthesized PCM into the cellular uplink without acoustic
  speaker-to-microphone coupling;
- run full duplex so caller speech continues while the agent speaks;
- stop unplayed agent audio immediately when the caller interrupts;
- recover after app, cable, Mac, or phone restarts;
- produce measurable call, audio, latency, and failure telemetry;
- conduct open-ended, multilingual, context-aware conversation rather than a
  fixed question/answer script;
- understand corrections, hesitations, accents, code-switching, names,
  numbers, intent, and vocal cues as far as the selected models allow;
- reason deeply, retrieve knowledge, and use approved tools without blocking
  the real-time audio loop;
- respond incrementally with natural speech and sub-second perceived latency;
- distinguish a real interruption from noise or a conversational backchannel;
- ask for clarification or transfer safely when understanding is uncertain.

Call control alone is not completion. The system becomes a full call gateway
only after digital downlink, digital uplink, full duplex, and flushing are
proven during real cellular calls.

## 2. Architectural boundary

```text
Cellular caller
      |
      | carrier voice / VoLTE
      v
+-------------------------------+
| Rooted Redmi 12C              |
|                               |
| Default dialer + InCallService|
| Deterministic call controller |
| Privileged audio capture      |
| Telephony uplink injection    |
| Bounded media buffers         |
| Local watchdog and telemetry  |
+---------------+---------------+
                |
                | private USB/network transport
                | control events + framed PCM
                v
+-------------------------------+
| Mac AI service                |
|                               |
| Gateway session manager       |
| Reflex/VAD/interruption loop  |
| Pipecat frame pipeline        |
| Streaming speech/turn input   |
| Conversation coordinator      |
| Deep reasoning + tools/memory |
| Streaming natural speech      |
| Metrics, logs, and storage    |
+-------------------------------+
```

Android remains a small cellular and audio appliance. It must not contain
provider credentials, CRM logic, or general agent intelligence. The Mac must
not depend on MediaTek mixer details.

### Active scope lock — bounded Mac latency sprint

The verified phone gateway is frozen for this sprint. Do not modify
`android_service_apk/`, the APK/GSI/installers, Android audio routing or
buffers, PHAG framing, ports, call-control semantics, or device configuration.
Do not rebuild, reinstall, reboot, or otherwise alter the phone.

Implementation is confined to `ai_bridge/`, Mac-side configuration, tests,
benchmarks, artifacts, and this plan. `mac_client/` may change only to expose
existing timestamps without changing transport behavior. Real cellular calls
remain permitted as validation through the current working phone.

Phone work may be reopened only if repeatable end-to-end timestamps prove that
a phone-side defect—not STT, endpointing, LLM/TTS release, or Mac buffering—is
blocking the latency gate.

## 3. Current baseline — updated 2026-08-25

### Implemented and physically verified

- Root access through PHH/TrebleDroid on Android 14/API 34.
- ADB USB connectivity and local port forwarding.
- A privileged, persistent system APK with the reviewed permission allowlist.
- Verified default-dialer role and Telecom-backed `InCallService` call objects.
- HTTP call control on port `8765` with real status and failure reporting.
- Digital audio servers on ports `8766` and `8767`.
- Python call-control client and raw PCM socket bridge.
- Proven caller-only `VOICE_DOWNLINK` capture during a real GSM call.
- Proven Telephony TX digital injection heard by the remote participant.
- Proven simultaneous capture/injection and caller barge-in with a 101 ms
  caller-speech-onset-to-Android-flush acknowledgement.
- Persistent installation, role, permissions, service startup, and listeners
  proven after a separate full reboot without rerunning the installer.
- An authenticated PHAG v1 control server has been deployed as a recoverable
  `/data/app` update and verified live on port `8768`; wrong-key authentication
  fails, duplicate command IDs return the cached response, and unauthenticated
  HTTP mutations are refused after key provisioning.
- Real Pipecat input/output transports, bounded media queues, a per-call worker,
  and a production STT -> LLM -> TTS pipeline assembly.
- Mac-side Flux turn timing now records content-free start, partial-revision,
  eager-EOT, resumed, and final-EOT events, including eager-to-final lead time
  and exact normalized eager/final agreement.
- The Flux path now uses its authoritative external turn strategy directly,
  avoiding needless local Smart Turn/Silero loading, and preserves completed
  transcripts even when provider word confidence is absent.
- Safety-gated real-call capture, injection, and interruption probes.
- Vendor audio policy entries for `Voice Call In`, `Telephony Tx`, and
  `incall_music_uplink`.

### Remaining production gaps

- PHAG v1 framing/authentication, real active-call media, generation rejection,
  process/ADB recovery, and persistent-GSI reboot survival are closed on the
  tested phone/ROM. Wider device/ROM portability is not yet established.
- Natural AI calls now run through native Ollama and selectable Kokoro/Edge TTS.
  The former `qwen3.8:latest` calls measured 4.246--8.129 seconds per turn and
  Kokoro failed the listener quality check. The production default is now the
  much faster `qwen3.5:4b-mlx`, but it still needs a real multi-turn cellular
  call before the end-to-end latency gate can be re-evaluated. Edge quality
  feedback and repeated reliability calls also remain outstanding.
- The conversation coordinator owns response cancellation and urgent flush;
  the Mac now observes Flux eager/commit/resume revisions in shadow mode.
  Speculative response release remains deliberately unimplemented until real
  Flux traces prove enough eager-to-final lead to justify a commit gate.
  Backchannels, uncertainty, routing, and long-running tool policy remain open.
- Initial provider latency and one real-cellular listener comparison exist, but
  the fixed corpus for required languages, accents, noisy speech, and
  code-switching has not been run.
- Security, long-duration drift, failure injection, and 100-call soak gates
  remain open.

## 4. Non-negotiable acceptance gates

### Gate A — deterministic cellular control

Pass only when:

- the gateway is the verified `ROLE_DIALER` holder;
- `InCallService` receives real incoming and outgoing `Call` objects;
- dial, answer, reject, hangup, and DTMF report the actual resulting state;
- emergency automation is refused;
- 50 consecutive test calls finish without stock-dialer UI automation.

### Gate B — caller-only digital downlink

Pass only when a remote test phrase arrives at the Mac as PCM and measurements
show:

- no silence or permission denial;
- no dependence on the Redmi microphone;
- no material copy of injected agent speech;
- stable format, cadence, and clock behavior;
- acceptable clipping, SNR, gaps, and drift.

### Gate C — direct digital uplink injection

Pass only when a remote recording endpoint hears injected test PCM while:

- the Redmi speaker and microphone are physically isolated or muted;
- the signal is traceable to the selected telephony TX route;
- gain is controlled and unclipped;
- sustained playback reports bounded underruns.

Local earpiece or speaker playback is not a pass.

### Gate D — full duplex and barge-in

Pass only when the caller speaks over agent playback and:

- downlink PCM continues arriving;
- local VAD detects caller onset;
- LLM and TTS work is cancelled;
- Mac queues and Android/HAL queues are flushed;
- late frames from the previous generation are rejected;
- caller-onset-to-agent-mute is P95 at or below 150 ms for the production
  target, with 250 ms retained only as the first integration ceiling.

The existing 101 ms flush-ack test is a strong candidate pass on the tested GSM
route, but the P95 target still requires a repeated corpus rather than one call.

### Gate E — unattended reliability

Pass only after:

- 30 minutes of stable full-duplex audio;
- 100 consecutive inbound/outbound call scenarios;
- recovery from Android process kill, Mac restart, USB reconnect, and phone
  reboot;
- bounded memory, queues, logs, and recordings;
- safe behavior for provider and tool failures.

### Gate F — broad conversational understanding

Pass only when a fixed, versioned telephone corpus demonstrates:

- accurate open-ended multi-turn conversation rather than scripted intents;
- successful handling of corrections, false starts, long pauses, ambiguity,
  spelling, names, numbers, dates, and addresses;
- acceptable performance for every required language, accent, and
  code-switching pair;
- conversation-state consistency over long calls;
- explicit clarification when confidence or tool arguments are insufficient;
- no claim that an external action succeeded before the tool confirms it;
- safe escalation or refusal when the request is unsupported.

### Gate G — conversational latency and voice quality

Initial production targets, measured at the remote telephone endpoint:

- caller speech onset to audible agent mute: P95 <= 150 ms;
- confirmed end of ordinary user turn to first audible agent speech:
  P50 <= 500 ms and P95 <= 900 ms;
- no stale audio from a cancelled generation;
- stable loudness, no clipping, bounded gaps/underruns, and intelligible
  pronunciation through the carrier codec;
- no regression beyond the carrier's GSM/VoLTE bandwidth ceiling.

These are acceptance targets, not promises derived from provider marketing.

## 5. Android gateway implementation

### 5.1 Default dialer and Telecom ownership

Implement a minimal `DialerActivity` that handles `ACTION_DIAL`, requests
`RoleManager.ROLE_DIALER`, shows gateway readiness, and provides manual recovery
controls. Declare an `InCallService` that satisfies the role contract and
provides a minimal incoming/ongoing UI path.

Use only Telecom APIs for production control:

- `TelecomManager.placeCall()` for outbound calls;
- `Call.answer()` for inbound calls;
- `Call.reject()` or `Call.disconnect()` for rejection/termination;
- `Call.playDtmfTone()` followed by `stopDtmfTone()`;
- registered `Call.Callback` instances for state changes.

Shell `input keyevent` and Activity Manager dialing remain diagnostic fallbacks.

Implement the state machine:

```text
OFFLINE -> READY -> RINGING -> ANSWERING -> ACTIVE
READY -> DIALING -> CONNECTING -> ACTIVE
ACTIVE -> ENDING -> READY
any state -> DEGRADED -> RECOVERING -> READY/OFFLINE
```

Every mutating command carries `command_id`. Store a bounded recent-command
cache so reconnect/retry cannot answer twice, dial twice, or repeat DTMF.

### 5.2 Foreground and boot lifecycle

- Start foreground mode before long-running initialization.
- Use API-34 foreground service types and permissions only when their runtime
  prerequisites are satisfied.
- Verify the dialer role before requesting `phoneCall` service behavior.
- Start after boot only if setup is complete.
- Use `START_STICKY`, but reconstruct live call state from Telecom after restart.
- Use wake locks only during active calls, reconnects, and short recovery work.
- Exempt the appliance from vendor battery optimization during provisioning.
- Never reboot automatically during a live call.

### 5.3 Privileged installation

The production gateway should be preinstalled as a reviewed privileged app or
delivered through a controlled system overlay/module. Add a narrowly scoped
`privapp-permissions` file and verify every effective permission after boot.

Candidate privileged permissions must be proven necessary on the exact ROM:

- `CAPTURE_AUDIO_OUTPUT` for voice-call capture;
- `MODIFY_AUDIO_ROUTING` for explicit telephony routes/audio policy;
- `MODIFY_PHONE_STATE` where the in-call music/telephony TX policy requires it;
- normal/dangerous call, state, microphone, foreground, boot, and network
  permissions already used by the gateway.

Do not treat a successful `pm grant` command as proof. Verify with `dumpsys
package`, AppOps, and a real audio operation.

### 5.4 Downlink capture ladder

Test one path at a time and record structured evidence:

1. `AudioRecord` with `VOICE_DOWNLINK` at the actual native call rate.
2. `AudioRecord` with `VOICE_CALL`, then quantify whether it is mixed TX+RX.
3. Explicit preferred `AudioDeviceInfo.TYPE_TELEPHONY` selection.
4. Vendor/TinyALSA capture only after mapping live PCM devices during a call.
5. Audio Policy/HAL changes only if the existing privileged routes cannot work.

Never hardcode `/dev/snd/pcmC0D*` from idle enumeration alone. Determine the
active route using policy, AudioFlinger/HAL logs, PCM state, and controlled
signal tests during CS and VoLTE calls.

### 5.5 Uplink injection ladder

Test in this order:

1. Android's privileged PSTN call-injection/redirection path where present.
2. A linear PCM music `AudioTrack` explicitly selecting the Telephony TX output
   device, allowing policy to select `AUDIO_OUTPUT_FLAG_INCALL_MUSIC`.
3. The MediaTek `incall_music_uplink` vendor route.
4. A small native root audio service using the proven vendor PCM/HAL route.
5. A device overlay or HAL patch as the final device-specific option.

The network format starts as signed PCM16 little-endian mono. The device must
report the actual capture and injection sample rates. Resample exactly once at
the boundary that needs it.

### 5.6 Bounded media engine

- Transport frames: 20 ms.
- Downlink queue target: 20-60 ms; hard maximum: 120 ms.
- Uplink queue target: 40-80 ms; hard maximum: 120 ms.
- Drop stale frames instead of playing them late.
- Track sequence gaps, clock drift, underruns, overruns, and queue depth.
- Provide `flush(generation_id)` and a `flush_ack` carrying the last accepted and
  last rendered sequence numbers.
- Recreate audio objects on every call until route persistence is proven safe.

## 6. Gateway protocol

Use a persistent authenticated control channel and independent media directions
so media backpressure cannot delay hangup or flush.

### 6.1 Control envelope

```json
{
  "v": 1,
  "type": "call.answer",
  "phone_id": "redmi12c-01",
  "call_id": "01J...",
  "command_id": "01J...",
  "seq": 42,
  "monotonic_ms": 12345678,
  "payload": {}
}
```

Required families:

- `gateway.hello`, `gateway.ready`, `gateway.health`;
- `call.incoming`, `call.dial`, `call.answer`, `call.reject`, `call.hangup`,
  `call.state`;
- `audio.start`, `audio.format`, `audio.flush`, `audio.flush_ack`, `audio.stop`;
- `dtmf.send`, `command.ack`, `error`, `warning`, `metrics.batch`.

### 6.2 Binary audio header

Each frame carries:

- protocol version and direction;
- `call_id` and `generation_id`;
- monotonically increasing sequence number;
- monotonic capture or intended-playback timestamp;
- sample rate, channel count, encoding, and payload length;
- PCM payload.

Reject malformed, oversized, replayed, wrong-call, and stale-generation frames.

### 6.3 Link and security

- Development: loopback listeners exposed only through ADB forwarding.
- Production: private USB Ethernet or dedicated private LAN.
- Authenticate the Mac with a pinned device key or mutual TLS.
- Do not expose ADB or gateway sockets on untrusted networks.
- Keep AI/provider credentials exclusively on the Mac.

## 7. Mac AI service

### 7.1 Session manager

Create one session object per call. It owns:

- gateway connection and call state;
- STT and TTS provider sessions;
- LLM context and approved tool state;
- one cancellation scope;
- current audio generation ID;
- structured logs and metrics;
- recording/transcript policy.

It also owns exactly one **conversation coordinator**. The coordinator is the
only component allowed to decide which generated response reaches the caller.
This prevents a fast model, a deep model, and a tool result from speaking over
one another. It routes each turn to either the production cascade or an
approved native speech-to-speech backend, but never lets both own the mouth.

Prewarm STT/TTS/LLM connections in `ANSWERING` or `CONNECTING`. Do not answer an
incoming call unless the Mac reports ready or an explicit fallback policy says
otherwise.

### 7.2 Pipecat production pipeline

```text
gateway input
 -> PhoneAgent Pipecat input transport
 -> frame/clock/call/generation validator
 -> caller-only processor
 -> local reflex VAD
 -> streaming speech understanding + semantic turn strategy
 -> user context aggregator
 -> conversation coordinator
 -> streaming LLM/reasoner + approved tools/memory
 -> streaming TTS
 -> generation filter
 -> PhoneAgent Pipecat output transport
 -> assistant context aggregator
```

Pipecat is the selected production orchestration framework. Implement real
transport processors based on Pipecat's input/output transport classes; the
current queue/callback file is only a placeholder. Keep the socket receive loop
free of STT, database, tool, and model latency. Section 15 defines the complete
framework decision and implementation contract.

### 7.3 Interruption contract

On likely caller speech start while the agent is speaking, the local reflex
path must immediately mute output. The semantic interruption classifier may
subsequently decide whether the event was a true interruption, a backchannel,
or a false positive. For a true interruption, concurrently:

1. emit the Pipecat interruption system frame;
2. cancel the in-flight LLM stream;
3. cancel/ignore the current TTS generation;
4. clear Pipecat transport output;
5. increment `generation_id`;
6. send Android `audio.flush`;
7. reject every late frame from the old generation;
8. record the spoken prefix rather than the entire generated answer.

If the event is a false interruption, the coordinator may resume only from a
known safe boundary. It must never replay a sentence the caller already heard
or release audio from the cancelled generation.

### 7.4 Provider boundaries

Define provider interfaces before choosing credentials:

- STT: streaming PCM input, partial/final transcripts, turn events, metrics;
- LLM: streaming text/tool calls, cancellation, usage metrics;
- TTS: streaming PCM output, generation IDs, cancellation, pronunciation
  controls;
- Tools: schemas, authorization, deadlines, and idempotency keys.

Also define a native realtime speech interface:

- duplex model: continuous PCM input, continuous PCM output, interruption and
  speaking-state events, text/tool side channel where supported, cancellation,
  and latency/usage metrics.

Initial provider candidates include Deepgram Flux, local MLX Whisper, the
authenticated local Antigravity Live Speech bridge (`StreamAudioTranscription`
Connect RPC for real-time word-by-word streaming), and the multimodal Gemini
3.7 Flash cascade path (`SendUserCascadeMessage` media parts for zero-shot
entity/fact confirmation). Provider selection must be validated on real
cellular call recordings.

### 7.5 Three concurrent conversational loops

The Mac runtime is not a single serial chain. It runs three coordinated loops:

1. **Reflex loop:** local audio/VAD, interruption, generation advance, queue
   clearing, and Android flush. It never waits for a cloud model.
2. **Conversation loop:** streaming understanding, adaptive end-of-turn,
   incremental reasoning, and streaming speech for ordinary turns.
3. **Deliberation loop:** retrieval, large-model reasoning, and tools that may
   take longer. It can authorize a short acknowledgement while work continues,
   but it cannot speak independently of the coordinator.

### 7.6 Intelligence policy

- Start with a high-intelligence streaming STT/LLM/TTS cascade as the
  production baseline because it offers the strongest controllability,
  transcripts, multilingual testing, tool use, and model replacement.
- Maintain a native full-duplex speech-to-speech adapter for controlled A/B
  tests with Moshi, PersonaPlex, or later models.
- Do not treat a small native speech model as automatically more intelligent
  because it speaks faster or handles overlap naturally.
- Preserve a parallel transcript/event record even when a native speech model
  is used, subject to recording and privacy policy.
- Route difficult turns to deeper reasoning without blocking the audio/reflex
  loop; use a natural acknowledgement when tool or retrieval latency is long.

## 8. Observability

Every call emits a structured timeline:

```text
call_ringing
call_answer_requested
call_active
audio_downlink_started
audio_uplink_started
stt_connected
tts_connected
user_speech_started
interruption_candidate
interruption_classified
user_turn_ended
turn_completion_probability
speculative_generation_started
speculative_generation_committed_or_discarded
llm_first_token
tts_first_audio
android_first_audio_written
android_playback_ack
user_interrupted
audio_flush_ack
tool_started
tool_confirmed_or_failed
call_ended
```

Measure P50/P90/P95/P99 for end-of-turn-to-first-audio and
speech-onset-to-mute. Also measure provider setup, STT entity accuracy, false
turns, queue depth, dropped frames, xruns, CPU, memory, temperature, tool
results, and cost per successful call/minute.

For every assistant turn, store the selected pipeline/model route, call and
generation IDs, what text/audio was generated, what prefix was actually heard,
why a response was cancelled, and whether any speculative compute was wasted.
Provider-reported latency is useful diagnostic data but remote-endpoint timing
is the acceptance authority.

## 9. Test program

### Unit and protocol

- State-machine transitions and invalid transitions.
- Duplicate command idempotency.
- JSON envelope validation.
- Binary frame round trips and malformed headers.
- Sequence gaps, stale generations, and wrong-call frames.
- Ring-buffer bounds and overflow policy.
- Tool authorization and idempotency.

### Device audio

- Known waveform Mac -> Android -> remote recording.
- Remote waveform -> Android -> Mac capture.
- Uplink/downlink isolation and echo leakage.
- Frequency response, clipping, SNR, cadence, gaps, and drift.
- Simultaneous caller speech and AI audio.
- Repeated flush during long playback.
- CS, VoLTE, weak signal, and route changes where supported.

### Conversation

- Greetings and short turns.
- Long pauses and self-corrections.
- Barge-in at start/middle/end of agent speech.
- Background speech and television.
- Names, numbers, addresses, dates, spelling, and confirmations.
- Tool success, slowness, timeout, duplicate retry, and partial failure.
- Required languages, accents, and code-switching.
- Backchannels such as "okay" or "uh-huh" that should not always terminate the
  current agent sentence.
- Overlapping speech, side conversations, ambient voices, and false VAD events.
- Open-domain questions, ambiguity, unknown facts, and clarification behavior.
- Long-context consistency and corrections to facts stated earlier in a call.
- Native speech-to-speech versus cascaded pipeline A/B runs on identical audio.

Use the open-source Full-Duplex-Bench suites as one reproducible layer for
turn-taking, overlap, disfluency, and tool-use evaluation. Add a PhoneAgent
cellular corpus because browser/studio audio does not reproduce GSM/VoLTE
codec loss, carrier jitter, or telephone loudness. Never promote a model based
only on a polished demonstration.

### Reliability

- Android gateway process kill.
- Mac process restart.
- USB/network disconnect during idle and active call.
- Phone and Mac reboot.
- Provider and tool failure injection.
- Low storage, high CPU, temperature, and weak carrier signal.

## 10. Delivery phases

### Phase 0 — baseline and diagnostics

Deliver:

- reproducible device inventory;
- documented current permissions, roles, policy, and HAL;
- safe diagnostic endpoints and structured logs;
- fixed local unit-test baseline.

Exit: the test environment and current behavior are reproducible.

### Phase 1 — production call control

Deliver the default dialer, Telecom state machine, API-34 service lifecycle,
idempotent commands, and manual recovery UI.

Exit: Gate A passes.

### Phase 2 — privileged audio feasibility

Deliver privileged installation, capture/injection matrix, remote recordings,
full-duplex tests, and measurable flush.

Exit: Gates B, C, and D pass. Stop or explicitly approve a HAL project if they
cannot pass with the available routes.

### Phase 3 — real-time gateway protocol

Deliver authenticated persistent control, framed media, bounded queues,
generation cancellation, metrics, and reconnect behavior.

Exit: 30-minute full-duplex transport with no unbounded drift.

### Phase 4 — baseline AI conversation

Deliver the real Pipecat transport, conversation coordinator, streaming
STT/LLM/TTS production cascade, per-call isolation, warm provider connections,
and basic inbound/outbound conversation.

Exit: repeatable natural multi-turn calls with no stale generation leakage and
complete turn timelines.

### Phase 5 — low-latency turn-taking

Deliver the three-loop runtime, local VAD, semantic interruption
classification, adaptive/confirmed end-of-turn, safe speculative generation,
full cancellation propagation, latency dashboards, Full-Duplex-Bench scenarios,
and a native speech-to-speech experimental adapter.

Exit: Gates F and G pass across the fixed cellular and benchmark corpora.

### Phase 6 — business agent

Deliver approved tools, deterministic business state, caller-context prefetch,
deadlines, idempotency, escalation, and multilingual behavior as required.

Exit: the selected business workflow completes accurately.

### Phase 7 — unattended production pilot

Deliver launchd/init supervision, watchdogs, fallback providers, cached critical
phrases, retention controls, security review, and failure/soak testing.

Exit: Gate E and the pilot compliance requirements pass.

## 11. Immediate implementation backlog

The previous phone-side feasibility backlog is complete on the tested GSM route.
The active backlog and verified status are now:

1. **Implemented and active-call verified.** Versioned control/audio
   envelope, call ID, sequence, timestamp, generation, acknowledgements, errors,
   HMAC-SHA256, and a Java/Python golden-vector contract.
2. **Implemented and fully recovery/reboot verified.** Link-epoch
   authentication, a crash-safe bounded command replay journal, ADB forwarding,
   exponential reconnect supervision, and monotonic generation
   resynchronization survived Android process death, full Mac ADB-daemon loss,
   persistent-system replacement, and an unattended reboot while Telecom
   remained idle. Work Package A is closed on the tested phone/ROM.
3. **Implemented and real-call verified.** Real Pipecat input/output transports
   provide bounded exact 20 ms PCM ingress, paced output, and authenticated
   frame identity.
4. **Implemented and Pipecat cellular verified.** A standard Pipecat
   `InterruptionWorkerFrame` advances generation and sends urgent authenticated
   `audio.flush`; Android reports last accepted/rendered sequences and rejects
   stale generations. The real Pipecat cellular probe measured a 38.46 ms
   caller-onset-to-flush acknowledgement, advanced to generation 3, and proved
   rejection of a deliberately late cancelled-generation frame by increasing
   Android's stale counter from 1 to 2. Work Package B is closed on the tested
   GSM route.
5. **Baseline implemented.** Per-call session manager, one worker per call, and
   response cancellation/flush coordinator. Advanced single-mouth policy is
   still part of item 7.
6. **Baseline implemented and real-call verified; quality gate remains open.**
   The fully local MLX-Whisper -> native no-thinking Ollama -> Kokoro cascade
   completed four assistant turns on a real cellular call. A second Edge neural
   TTS call completed one caller/assistant turn. Context, streaming media,
   generation cancellation, and speech return are proven, but measured
   user-to-bot latency and Kokoro audible quality do not yet pass production.
7. **D1 measurement foundation implemented; Mac only.** The existing Pipecat
   Flux path now has a thin revision-aware `LISTENING`, `EAGER`, `RESUMED`, and
   `COMMITTED` tracker. It records content-free turn timing and eager/final
   agreement without changing the final transcript -> LLM path or releasing
   speculative audio. Flux final transcripts are no longer silently discarded
   when confidence metadata is absent.
8. **Active next measurement.** With provider credentials available, replay at
   least 20 fixed recorded turns through existing Deepgram Flux and collect
   eager-to-final P50/P95, resume rate, eager/final agreement, final-EOT delay,
   and false endpoints. Then run three controlled multi-turn cellular calls
   through the unchanged phone.
9. **Zero-Credential Local STT Pipeline Integration.** Integrate the local
   authenticated Antigravity Live Speech bridge (`StreamAudioTranscription`)
   as a native Pipecat `STTService` (`antigravity_live`) to provide streaming
   Google-grade speech recognition on cellular downlinks with no third-party
   cloud credentials.
10. **A/B only existing integrations.** Compare existing Cartesia token-streaming
    TTS against the Edge baseline on identical text and the cellular route. Do
    not add another framework or provider adapter during D1. If credentials are
    unavailable, record the limitation instead of building a fragile substitute.
11. **Conditional—not automatic.** Implement cancellable eager Ollama generation
    and a Mac-side speech commit gate only if item 8 proves a material reusable
    lead. Require zero duplicate responses and zero uncommitted audio leakage.
12. Memory, retrieval, tools, advanced backchannels, native duplex models, and
    dashboard UI are deferred until D1 selects one measured next bottleneck.
13. Security, failure injection, long-duration duplex, and the 100-call soak
    remain later production gates, not part of this bounded latency sprint.

## 12. Definition of done

PhoneAgent is complete only when an unattended inbound or outbound cellular
call can be controlled by the Mac AI, the remote caller and AI exchange clean
digital full-duplex audio, interruption flushes within target, failures are
safe, and the AI can sustain broad, context-consistent, high-quality
conversation with approved tools and explicit uncertainty handling. Completion
must be demonstrated by repeatable cellular tests, Full-Duplex-Bench-style
evaluation, tool-result verification, and telemetry rather than API response
shapes or one impressive demonstration.

## 13. Implementation checkpoint — 2026-08-25

Implemented and verified:

- minimal Android dialer/recovery activity and `ROLE_DIALER` assignment;
- Telecom-backed call controller with reject support, expanded states, DTMF
  validation, single-call protection, and emergency-number refusal;
- API-34-safe foreground startup with setup fallback;
- Android-safe loopback HTTP server replacing the desktop JDK HTTP dependency;
- `/health`, `/audio/status`, `/audio/flush`, and stricter call endpoints;
- loopback audio servers on ports `8766` and `8767`;
- privileged downlink candidates beginning with `VOICE_DOWNLINK`;
- explicit Telephony TX selection with no misleading speaker fallback;
- provisional generation increment and Android `AudioTrack` flush;
- bounded/thread-safe Mac audio buffering and repaired reconnect lifecycle;
- Mac health/reject/audio-status/audio-flush APIs;
- a safety-gated real-call WAV/tone probe;
- a race-free, safety-gated live-call capture runner that owns dialing, waits
  for `ACTIVE`, attaches the downlink socket in the same process, stops on
  remote disconnect, and guarantees a hang-up attempt during cleanup;
- a persistent GSI builder and guarded fastbootd flasher that embed the APK and
  privileged allowlist into the actual ext4 `system_a` image while preserving
  the untouched original image and a hash receipt for rollback;
- offline test selection that excludes live telephony mutations by default.
- pinned Python 3.12/Pipecat 1.7 project environment with cloud and test extras;
- authenticated version-1 media/control frame codec with hard payload bounds,
  incremental TCP decoding, call/generation/sequence identity, and HMAC-SHA256;
- thread-safe per-call session state, generation advancement, bounded media
  ingress, paced output, and interruption-to-phone flush coordination;
- real Pipecat input/output transports, production cascade assembly, content-free
  turn/latency telemetry, and per-call worker lifecycle;
- selectable LLM factory for local Ollama, OpenRouter, OpenAI, Gemini, and the
  locally authenticated Codex app-server;
- native Ollama `/api/chat` streaming with explicit `think=false`, numeric
  keep-resident policy, exact-context prewarming, cancellation, NDJSON bounds,
  and Pipecat lifecycle/usage frames; the old OpenAI-compatible Ollama adapter
  is no longer used;
- selectable Cartesia, local Kokoro, and Edge neural TTS; Edge uses bounded
  phrase aggregation plus one continuous cancellable FFmpeg MP3-to-PCM decoder
  per phrase so network chunks cannot become phone-audio cuts;
- Ollama default bound to the installed `qwen3.5:4b-mlx` model, with explicit
  non-thinking voice sampling, exact-context prewarming, and measured native
  warm behavior;
- a credential-safe Codex app-server JSON-RPC client and Pipecat LLM service
  using ephemeral, read-only, tool-disabled threads, streamed message deltas,
  timeout, and turn interruption;
- provider objects constructed before call attachment, Ollama and MLX Whisper
  prewarmed before gateway readiness, and Pipecat DEBUG transcript logging
  disabled by the production INFO default;
- 47 passing offline tests covering the client SDK, audio boundary, protocol,
  provider credential policy, native Ollama streaming, Edge decoding and
  cancellation, configuration, local app context conversion, and Flux turn
  revisions/timing without retaining transcript content;
- local Antigravity live speech transcription reverse-engineering analyzed,
  benchmarked, and architected as a zero-credential streaming Pipecat STT service
  (`antigravity_live`) for cellular downlinks.

Live device verification currently shows:

- Android 14/API 34 on MT6768;
- gateway holds `ROLE_DIALER`;
- APK is registered from `/system/priv-app/PhoneAgentGateway` in the live
  userdebug overlay;
- `CAPTURE_AUDIO_OUTPUT`, `MODIFY_AUDIO_ROUTING`, `MODIFY_PHONE_STATE`, and
  `CONTROL_INCALL_EXPERIENCE` are effectively granted;
- control, downlink, and uplink sockets are listening;
- Android reports a Telephony TX output device;
- a controlled outbound GSM call was placed through the Mac gateway, reached
  `ACTIVE`, remained active for about 14 seconds, and ended with Telecom's
  normal `REMOTE` disconnect cause. This proves the outbound Telecom control
  path end to end;
- a second controlled GSM call attached the digital capture socket about 71 ms
  after `ACTIVE`; Android selected `VOICE_DOWNLINK` and produced an 18.34-second
  PCM16/16 kHz/mono WAV containing 586,880 audio bytes. Signal analysis found
  substantial non-silent energy and no clipped samples. This passes the
  controlled remote-call downlink-capture proof on the current GSM route;
- a Gate C candidate test sent a complete 1-second/1 kHz/32,000-byte PCM signal
  while downlink capture continued. Android selected the Telephony TX device,
  counted all 32,000 uplink bytes, acknowledged generation-2 flush, showed no
  strong tone loopback into `VOICE_DOWNLINK`, and returned cleanly to `IDLE`;
  the remote participant confirmed hearing the beep. This passes Gate C digital
  uplink injection on the current GSM route;
- a Gate D candidate test kept `VOICE_DOWNLINK` capture active during a planned
  3-second injected tone. Two consecutive 20 ms caller-speech frames crossed
  the -42 dBFS threshold, stopped production after 2.46 seconds, and received a
  generation-aware Android flush acknowledgement 101 ms after measured speech
  onset. The remote participant confirmed the beep audibly stopped when they
  spoke. This passes Gate D full-duplex interruption on the current GSM route;
- the persistent image was flashed only to logical `system_a` through userspace
  fastbootd. A subsequent normal reboot—with no installer, permission grant,
  role assignment, or service-start command—preserved the `/system/priv-app`
  package, allowlist hashes, four privileged grants, dialer role, automatic
  `BOOT_COMPLETED` service startup, three phone-side listeners, and clean
  gateway health. Phone-side privileged persistence is therefore proven.
- the first natural fully local cellular AI call completed four assistant
  turns through MLX Whisper, native no-thinking Ollama, and Kokoro. Authenticated
  interruption advanced generation 1 to generation 5. Four measured
  user-to-bot latencies ranged from 4.246 to 8.129 seconds with a 5.801-second
  median. Kokoro token synthesis was audibly poor according to the remote user
  and produced one punctuation-only error, so this is functional evidence, not
  a quality pass;
- a subsequent Edge neural TTS cellular call completed one caller/assistant
  turn. The measured turn was 5.723 seconds user-to-bot, including 1.459 seconds
  MLX STT first-result time, 2.078 seconds waiting for a safe Ollama phrase, and
  580 ms Edge first audio. The caller's subjective cut/quality verdict remains
  to be recorded;
- an external authenticated control client rotated the host's link epoch while
  idle; the repaired host treated the stale-epoch rejection as reconnectable
  and restored control in about 106 ms without exiting.

Still required after the authenticated-protocol, Pipecat, recovery, and
persistence checkpoints below:

- repeat controlled Edge phrase-streaming calls and record the remote listener's
  quality/cut verdict; compare supported Azure/Cartesia if Edge is unreliable;
- complete low-latency endpointing, reconnect/failure injection, quality,
  understanding,
  business-workflow, security, and soak phases.

### Authenticated protocol checkpoint — 2026-08-25

Implemented in this checkpoint:

- `ProtocolCodec.java`, byte-for-byte compatible with the Python PHAG v1 codec,
  including exact big-endian headers, bounded payloads, HMAC-SHA256 verification,
  constant-time tag comparison, and authenticated JSON/audio frames;
- root-provisioned 32-byte link-key storage in Android app-private storage and a
  Mac key file with restrictive permissions; provisioning compares hashes and
  never prints key material;
- a persistent framed control server on port `8768`, independent of both media
  directions, with link-epoch/call binding, UUID command IDs, a bounded 256-entry
  replay cache, ACK/error frames, call control, DTMF, status, health, and urgent
  generation-aware audio flush;
- Android ports `8766` and `8767` migrated from raw PCM to authenticated PHAG v1
  audio frames, exact 20 ms PCM validation, sequence tracking, stale-call/epoch/
  generation rejection, and last-accepted/last-rendered sequence reporting;
- `FramedGatewayLink` on Mac with independently backpressured control/downlink/
  uplink sockets, authenticated handshakes, automatic ADB forwarding, hard send
  failures, exponential reconnect, link-epoch replacement, and generation
  resynchronization that never moves backwards;
- `AuthenticatedPhoneAgentClient` for idempotent framed call operations;
- framed media identity integrated into `PhoneAgentTransport`; raw TCP chunks no
  longer invent generation/sequence identity on the production path;
- `phone-agent-voice` replaced with an asynchronous per-call host that respects
  the configured auto-answer policy, attaches media only after `ACTIVE`, starts
  one `ProductionCallPipeline`, greets only after media readiness, and closes the
  worker/session/link at call end;
- a fixed Java/Python binary/HMAC golden vector plus offline fake-gateway tests
  for three-channel authentication, media routing, idempotency, and reconnect.
- a real `PipelineWorker` loopback that starts the complete Pipecat transport
  lifecycle, receives authenticated caller frames, drives synthetic agent PCM to
  the uplink handler, maps `InterruptionWorkerFrame` through the worker to a
  generation-2 flush, and rejects a late generation-1 input frame. This test
  exposed and fixed a method-name
  collision that the earlier direct transport tests could not detect.

Verification recorded for this checkpoint:

- `29 passed, 4 deselected` in the safe Python suite;
- the complete project passes Ruff with no remaining findings;
- the Android API-34 APK compiles, dexes, aligns, and signs successfully;
- the Java codec encodes and decodes the Python golden vector exactly;
- the connected Android phone runs the new APK as a recoverable data update,
  retains `ROLE_DIALER` and all four reviewed privileged grants, and listens on
  ports `8765` through `8768`;
- authenticated live `gateway.health` and `call.status` returned `ready`/`IDLE`;
- a deliberately wrong HMAC key was rejected;
- two live `gateway.health` commands with the same UUID returned the same cached
  acknowledgement;
- unauthenticated HTTP `POST /audio/flush` returned HTTP 426 without mutating
  the generation.
- a controlled call to the consenting test number completed through PHAG v1:
  the Mac captured 95 authenticated 20 ms downlink frames (60,800 bytes/1.9 s),
  observed two caller-speech frames above -42 dBFS, received generation-2 flush
  acknowledgement 29.8 ms after measured speech onset, injected one deliberately
  late generation-1 frame, and observed Android's stale-frame counter increase
  from 0 to 1. Telecom returned to no active calls and the probe preserved WAV
  and JSON evidence under `artifacts/framed-calls/`.

This checkpoint proves the authenticated media/generation transaction on the
real GSM route. Work Package A remains open only for live USB/process reconnect
and resynchronization proof plus baking this APK into the GSI and repeating the
full reboot verification. The Pipecat cellular checkpoint below subsequently
closed Work Package B.

### Pipecat cellular checkpoint — 2026-08-25

The controlled call ran through a real Pipecat `PipelineWorker`, not the direct
protocol probe. Its production boundary was:

```text
Android framed downlink -> PhoneAgentTransport.input -> caller-audio sink
synthetic agent PCM -> PhoneAgentTransport.output -> Android framed uplink
InterruptionWorkerFrame -> generation advance -> authenticated audio.flush
```

Verified results for call `5f1d1f59-76c5-49bd-8782-e5cda89bf227`:

- the worker reached `on_pipeline_started` and delivered 84 authenticated
  caller frames (53,760 bytes, 1.68 seconds) into Pipecat;
- caller audio peaked at -11.42 dBFS and was deliberately consumed by the sink,
  so it could not echo back into the uplink;
- Pipecat sent three exact 20 ms output frames before caller speech triggered
  the standard worker interruption frame;
- the interruption advanced the synchronized generation to 3 and completed the
  phone flush acknowledgement 38.46 ms after measured caller-speech onset;
- one deliberately late old-generation frame increased Android's stale-uplink
  counter from 1 to 2, proving it was rejected rather than rendered;
- `EndFrame` stopped the worker cleanly, cleanup hung up the call, and Telecom
  reported no remaining active call.

Evidence is preserved at
`artifacts/pipecat-calls/pipecat-call-20260825-124659.json` and
`artifacts/pipecat-calls/pipecat-call-20260825-124659.wav`. The offline lifecycle
test also uses `InterruptionWorkerFrame`, matching Pipecat 1.7's documented
worker lifecycle. This achieves Work Package B's exit condition on the tested
GSM route. Representative-call quality, resampling tests, and P95 interruption
statistics remain later quality/latency gates, not blockers for this transport
integration milestone.

Post-checkpoint regression verification passed: project-wide Ruff, the safe
Python suite (`29 passed, 4 live tests deselected`), the Java/Python PHAG golden
vector, and a complete API-34 APK compile/dex/align/sign build. A final read-only
phone check reported `gateway=ready`, Telecom `IDLE` with no calls, generation
3, last accepted/rendered sequence 2, stale-uplink count 2, and no audio error.

### Work Package A live recovery checkpoint — 2026-08-25

The Android command replay cache is now a bounded, crash-safe journal in app
private storage rather than process memory alone. Before executing a command,
Android synchronously persists an in-progress marker. A crash after a possible
side effect but before final-result persistence therefore produces a safe
"outcome uncertain" response instead of repeating the mutation. Completed
results survive process death, and each UUID is bound to its original command
type and payload.

The safety-gated idle-phone probe at `mac_client/recovery_probe.py` verified:

- the gateway process changed PID from 12125 to 12260 after `am force-stop` and
  explicit foreground-service recovery;
- authenticated process recovery completed in 1.638 seconds with a new link
  epoch;
- killing the complete Mac ADB daemon forced loss of the forwarded sockets,
  automatic ADB/forward recreation, and authenticated recovery in 3.544 seconds
  with another new link epoch;
- generation advanced from 1 to 2 before failure and remained 2 through both
  recoveries, proving that Android process reset could not move it backwards;
- replaying the same UUID after Android process death returned the exact
  persisted original result, while attempting a different command with that
  UUID was rejected;
- final Telecom state was `IDLE`; no cellular call was placed.

Evidence is preserved at `artifacts/recovery/live-recovery-proof.json`. This
closes live Android-process and ADB-transport recovery. At this checkpoint,
Work Package A waited only for the authenticated APK to replace the recoverable
update in the persistent GSI and survive an unattended reboot proof; the next
checkpoint records that completed proof.

### Current persistent GSI checkpoint — 2026-08-25

The current authenticated, crash-safe protocol APK was embedded into a fresh
clone of the untouched rollback image. The builder extracted the APK and
allowlist back out byte-for-byte, verified root ownership, `0755/0644` modes,
the exact `system_file` SELinux xattrs, fixed image size, and a clean read-only
`e2fsck`. The resulting image is:

```text
artifacts/persistent-gsi/system-phoneagent-phag-v1-20260825.img
SHA-256 56427c6c213ddec8b4e657198246bf4c8c16e37969248b2966177c88768d6290
```

The guarded flasher wrote only logical `system_a` and retained the pristine
rollback image with SHA-256
`0c3276a30b0a45b9b87eb629d50d90fb1dafbbb8e556f0b5dd60dcc4ef22d42d`.
It now explicitly removes only PhoneAgent's recoverable `/data/app` update,
requires Package Manager to fall back to the flashed system APK, reprovisions
and hash-checks the link key, and forwards authenticated control port `8768`.

The safety-gated unattended reboot proof issued no post-reboot phone install,
permission, role, key, or service-start repair. It verified:

- both pre- and post-reboot package paths were exactly
  `/system/priv-app/PhoneAgentGateway/PhoneAgentGateway.apk`;
- Android completed boot in 64.258 seconds and delivered `BOOT_COMPLETED` such
  that `GatewayService` plus listeners `8765`–`8768` were ready 4.317 seconds
  later;
- the dialer role, all four privileged grants, and the exact Mac/phone link-key
  hash survived;
- an authenticated command's persisted replay result survived the full reboot;
- generation remained 1 rather than moving backwards and Telecom returned
  `IDLE`.

Evidence is preserved at
`artifacts/persistent-gsi/persistent-reboot-proof.json`, and the flash receipt is
`artifacts/persistent-gsi/flash-receipt-20260825-130104.txt`. This closes Work
Package A on the tested Redmi/API-34/TrebleDroid configuration.

Framework research completed on 2026-08-25 selected Pipecat as the production
conversation runtime. LiveKit Agents remains a strong reference/alternative if
the media architecture later moves to WebRTC rooms or SIP. TEN Framework is not
selected because its heavier runtime is unnecessary for the direct USB PCM
topology and its repository license contains non-standard deployment
restrictions. Moshi, PersonaPlex, Ultravox, and newer native duplex projects are
model/backend candidates, not replacements for the session, security, tool,
and evaluation layers. Section 15 records the complete decision.

### Silent-uplink root cause and permanent fix — 2026-08-26

A live call reached `ACTIVE`, transcribed the caller correctly, produced correct
French responses, and reported "Played completely" in the Studio while the remote
party heard nothing at all. The phone's own counters showed `uplink_bytes=0`,
`audio_playout_frames=0`, `playout_acks_sent=0`, `injection_route="not_started"`,
and `last_error="Could not start Telephony TX AudioTrack after 3 attempts"`.

Root cause: `DigitalAudioBridge` leaked telephony `AudioTrack` objects.
`runContinuousPlayout` returned from `if (track != activeTrack) return;` without
releasing the track, and its blocking `AudioTrack.write` was held inside
`OUTPUT_LOCK`, so a stalled modem route blocked the cleanup that would have
released it while `interrupt()`/`join(500)` — which cannot unblock a blocking
write — timed out. `dumpsys media.audio_flinger` showed the
`AUDIO_OUTPUT_FLAG_INCALL_MUSIC` → `AUDIO_DEVICE_OUT_TELEPHONY_TX` thread holding
**40 of 40 active tracks**, 23 of them this app's 16 kHz injection tracks owned by
four dead PIDs (one process alone leaked 14). The output was saturated, no new
injection track could be created, and the MediaTek HAL logged
`AudioALSAStreamOut: write(), streamout flag:0x10000 should only write data
during phonecall` 13,711 times across 8 idle minutes. This is why Gate C passed
originally on a freshly booted phone and silently decayed afterwards.

Second defect: the Studio's playback status was derived from Pipecat's
`BotStartedSpeakingFrame`/`BotStoppedSpeakingFrame`, which `BaseOutputTransport`
emits from `_handle_bot_speech` *before* `write_audio_frame` is attempted and
regardless of its result. "Played completely" therefore meant only "TTS handed
audio to the output transport" and carried no information about the phone, which
is what made an eight-minute total failure look like a healthy call.

Fixes applied:

- the playout writer no longer abandons its track, and no longer holds
  `OUTPUT_LOCK` across a blocking HAL write;
- `streamUplink` owns the track for the whole connection and releases it on every
  exit path using stop → join → release, so the track is provably not in use
  before release (releasing under an in-flight native write is a use-after-free);
- the Telephony TX route is now started **before** the uplink handshake is
  acknowledged, so an unusable route fails the Mac's `connect_media()` instead of
  accepting a whole call into a dead socket;
- `PhoneVoiceAgent._start_call` verifies `tx_connected`/`injection_route` and
  raises a Studio `call_error` plus hangs up rather than running a silent call;
- `PlaybackEventProcessor` measures delivery from the session's transport
  counters and reports `not_delivered` ("NOT HEARD") when no frame reached the
  phone;
- the track-start failure now reports its real root cause chain;
- `build_and_install.sh` restarts `audioserver` after install, because installing
  force-stops the app and a SIGKILL never runs `onDestroy`.

Correction — 2026-08-26, later the same day. The first round of verification
above was measured with `grep -A95 "name AudioOut_25"`, an anchor that lands on
a different thread's track count. Those `0 Tracks` readings were an artifact and
the leak had **not** been closed. Re-measuring anchored on the
`AUDIO_OUTPUT_FLAG_INCALL_MUSIC` block showed one track leaking per uplink
connect/disconnect cycle, from the live gateway process, while `tx_connected`
was false.

The real mechanism is not the Java lifecycle, which runs correctly and logs no
release failure. Telephony TX only exists while a call is up; building a track
against it otherwise makes AudioFlinger log `restoreTrack_l: dead IAudioTrack,
PCM, creating a new one from setOutputDevice()` and orphan the original on the
INCALL_MUSIC output, so `release()` retires the replacement rather than the
orphan. Starting the route before the handshake had made this worse by creating
a track on every connection attempt, including ones with no call.

The uplink server now refuses to build a track unless Telecom reports `ACTIVE`.
Verified after redeploy with the corrected anchor: six consecutive uplink
connect/disconnect cycles with no call left `0` tracks each time, and the
handshake is refused rather than accepted, so the Mac gets an immediate
`connect_media` error instead of a silent call.

Still unproven: the in-call path, where a track legitimately is created. Whether
`release()` retires it cleanly during a live call has not been observed, so the
telephony track count must be checked after the next real cellular call. The
correct command anchors on the output flags, never on a thread name:

```bash
adb shell 'dumpsys media.audio_flinger' \
  | sed -n '/AUDIO_OUTPUT_FLAG_INCALL_MUSIC/,/^Output thread/p' \
  | grep -cE "^ +[0-9]+ +yes +[0-9]+"
```

`169 passed, 4 deselected`, Ruff clean on all changed files, and the Java/Python
PHAG golden vector still passes.

This work reopened `android_service_apk/` under the Section 2 scope-lock
exception: repeatable device evidence proved a phone-side defect — not STT,
endpointing, LLM/TTS release, or Mac buffering — was blocking the audio path.

### Local Parakeet speech recognition checkpoint — 2026-08-26

Section 15.16 forbade an `antigravity_app` provider built by scraping tokens and
cloning private ConnectRPC calls, yet `antigravity_live` had become the default
STT. It is also a cloud round trip: the bundled language server is started with
`--api_server_url https://generativelanguage.googleapis.com`, so every caller
turn depends on Google, on the Antigravity app being open and logged in, and on
an undocumented endpoint.

Measured on this M4 Max against the four fixed English/French phone clips, with
identical end-of-speech methodology for both engines:

| Engine | Post-speech to final turn | Turn splits | WER |
|---|---:|---:|---:|
| `antigravity_live` (cloud) | **1810.1 ms** | 0/4 | 0.000 |
| `parakeet_local` @ 700 ms endpoint | **707.2 ms** | 0/4 | 0.000 |

The cloud figure is 1810 ms on every clip because `_required_silence()` raises
the endpoint to `antigravity_live_fallback_endpoint_ms` (1800 ms) whenever the
provider has not finalized, and over a real-time feed it never finalizes first.
That floor is a direct consequence of depending on a provider final; a local
recognizer removes it. **Net saving is 1.10 s on the critical path of every
turn**, at identical accuracy.

Design decisions, each from measurement rather than assumption:

- **Batch on endpoint, not streaming.** Streaming at the library default
  `(256, 256)` context measured a real-time factor of 1.55 and falls behind the
  caller. A context/chunk sweep found cost is context-dominated (~185 ms per
  chunk at ctx 128 regardless of chunk length); the best streaming point was
  0.29. A single fully buffered pass with `keep_original_attention=True`
  measures 0.017–0.033 instead, and is more accurate because attention is not
  windowed.
- **Trim the endpoint pause before transcribing.** Buffering the silence made
  the buffer grow after speech ended, so the speculative pass never matched the
  committed audio and every turn paid a second inference. Trimming to the last
  energetic frame plus 120 ms makes the speculative result reusable, which is
  why post-speech latency is now the endpoint timer plus roughly 10 ms — the
  model cost hides entirely inside the pause.
- **Never await inference in the endpoint watchdog.** Awaiting the speculative
  pass inline delayed the endpoint decision by a whole model pass.
- **One owned inference thread.** MLX streams are thread-local, so
  `asyncio.to_thread` raised `There is no Stream(cpu, 1) in current thread`
  under the default executor. All MLX work runs on a single owned thread, which
  also serializes GPU use against local TTS.
- **Endpoint default 700 ms.** At 480 ms, 2 of 4 clips split one utterance into
  two caller turns. 700/1100 ms matches the already-tuned speculative constants
  and split nothing.

Scope: English and French only; the service refuses other locales. Parakeet v3
covers 25 European languages and has no Arabic, and on the real Darija call
recordings in `artifacts/` it returns phonetic nonsense where the Google bridge
returns correct text. `whisper_mlx` was also measured and is unusable for live
telephony: ~2.7 s per utterance regardless of length, and on the same Darija
recordings it auto-detected Russian and discarded nearly all content.

`parakeet_local` is now the default STT provider on the strength of the
measurements above. `antigravity_live` is retained unchanged and is selected
with `PHONE_AGENT_STT_PROVIDER=antigravity_live`; it remains the only supported
path for Arabic/Darija callers. The promotion is still pending its real-call
evidence: the accuracy above is from clean synthetic speech, not a GSM channel,
so the first English/French cellular call must confirm it before this default is
treated as proven. Licensing is clean for commercial use: parakeet-mlx is
Apache 2.0 and the model is CC-BY-4.0.

## 14. Rooted Android phone gateway — implementation and usage guide

This section is the practical companion to the architecture above. It explains
how this repository turns the tested rooted Redmi into a USB-connected cellular
gateway, how the Mac controls it, and where to modify the implementation.

### 14.1 What runs on each machine

```text
Android / SIM side                         Mac / AI side
------------------                         -------------
Telecom + cellular modem                   Call-session controller
PhoneAgent InCallService                   STT -> LLM -> TTS pipeline
CallManager                                VAD and interruption logic
AudioRecord(VOICE_DOWNLINK) -- PHAG/ADB --> Pipecat input / STT
Telephony TX AudioTrack     <-- PHAG/ADB -- Pipecat output / TTS
127.0.0.1:8765 diagnostics                Read-only HTTP diagnostics
127.0.0.1:8766 framed downlink            Authenticated RX channel
127.0.0.1:8767 framed uplink              Authenticated TX channel
127.0.0.1:8768 framed control             Authenticated command channel
```

The phone remains the cellular endpoint. The Mac never talks directly to the
carrier: ADB forwards four phone-loopback ports to Mac loopback. This avoids
opening an unauthenticated telephony service on Wi-Fi or the LAN.

### 14.2 Relevant source files

| Purpose | Project path |
|---|---|
| Android manifest and roles | `android_service_apk/AndroidManifest.xml` |
| Telecom call ownership | `android_service_apk/src/com/phoneagent/gateway/PhoneAgentInCallService.java` |
| Dial/answer/reject/hangup/DTMF | `android_service_apk/src/com/phoneagent/gateway/CallManager.java` |
| Read-only HTTP diagnostics | `android_service_apk/src/com/phoneagent/gateway/HttpServerEngine.java` |
| PHAG v1 codec | `android_service_apk/src/com/phoneagent/gateway/ProtocolCodec.java` |
| Authenticated control server | `android_service_apk/src/com/phoneagent/gateway/ProtocolControlServer.java` |
| Digital RX/TX audio | `android_service_apk/src/com/phoneagent/gateway/DigitalAudioBridge.java` |
| Foreground lifecycle | `android_service_apk/src/com/phoneagent/gateway/GatewayService.java` |
| Privileged allowlist | `android_service_apk/privapp-permissions-com.phoneagent.gateway.xml` |
| Build and normal update | `android_service_apk/build_and_install.sh` |
| Temporary live-overlay deployment | `android_service_apk/install_privileged.sh` |
| Persistent GSI builder | `android_service_apk/build_persistent_gsi.sh` |
| Guarded fastbootd deployment | `android_service_apk/flash_persistent_gsi.sh` |
| Authenticated three-channel Mac link | `mac_client/framed_link.py` |
| Authenticated call-control SDK | `mac_client/protocol_client.py` |
| Direct PHAG v1 hardware proof | `mac_client/framed_call_probe.py` |
| Pipecat cellular proof | `mac_client/pipecat_call_probe.py` |
| Production Pipecat transport | `ai_bridge/pipecat_transport.py` |
| Production per-call host | `ai_bridge/phone_voice_agent.py` |
| Legacy feasibility diagnostics only | `mac_client/gateway_client.py`, `mac_client/audio_bridge.py`, `mac_client/live_call_test.py` |

All commands below assume the current directory is:

```bash
cd /Users/aziz/Desktop/PhoneAgent/phone_agent_gateway
```

### 14.3 Android role and privileged permissions

The app must be the default dialer so Android binds its `InCallService` and
delivers real `Call` objects. The relevant manifest shape is:

```xml
<service
    android:name=".PhoneAgentInCallService"
    android:permission="android.permission.BIND_INCALL_SERVICE"
    android:exported="true">
    <intent-filter>
        <action android:name="android.telecom.InCallService" />
    </intent-filter>
    <meta-data
        android:name="android.telecom.IN_CALL_SERVICE_UI"
        android:value="true" />
</service>
```

Root alone does not grant voice-call capture. The APK is installed as a
privileged system app and allowlisted for the device-specific permissions that
were proven necessary:

```xml
<privapp-permissions package="com.phoneagent.gateway">
    <permission name="android.permission.CAPTURE_AUDIO_OUTPUT" />
    <permission name="android.permission.MODIFY_AUDIO_ROUTING" />
    <permission name="android.permission.MODIFY_PHONE_STATE" />
    <permission name="android.permission.CONTROL_INCALL_EXPERIENCE" />
</privapp-permissions>
```

Do not generalize this allowlist to unrelated apps. Re-audit it after changing
the ROM, Android version, device, or signing key.

### 14.4 Build and privileged deployment

Confirm that ADB sees exactly the intended test phone:

```bash
adb devices -l
adb shell getprop ro.build.version.sdk
adb shell getprop ro.build.type
adb shell su -c id
```

Build the APK without mutating the phone:

```bash
./android_service_apk/build_and_install.sh --build-only
```

For temporary development only, the live-overlay installer remains available:

```bash
./android_service_apk/install_privileged.sh --commit
```

That installer is useful for fast iteration but its GSI scratch overlay does
not mount during a normal boot on this device. Production persistence uses an
offline system-image build instead:

```bash
./android_service_apk/build_persistent_gsi.sh \
  --base-image /Users/aziz/Documents/PhoneAgent/scratch/gsi/system.img \
  --output artifacts/persistent-gsi/system-phoneagent-persistent.img
```

The builder never edits the base image. It clones the ext4 image, adds the APK
and allowlist, applies root ownership, `0755/0644` modes, and the exact
null-terminated `system_file` SELinux xattr copied from the base image. It then
dumps both embedded payloads back out, compares their SHA-256 hashes, runs
read-only `e2fsck`, and records the final image hash.

Flash only while the reviewed phone is idle and connected to power:

```bash
./android_service_apk/flash_persistent_gsi.sh \
  --serial rgr8r8zxmv9txgi7 \
  --image artifacts/persistent-gsi/system-phoneagent-phag-v1-20260825.img \
  --rollback-image /Users/aziz/Documents/PhoneAgent/scratch/gsi/system.img \
  --link-key /Users/aziz/.config/phone-agent/link.key \
  --commit
```

The guarded flasher performs these operations:

1. verifies API 34, `userdebug`, and root;
2. verifies the TrebleDroid fingerprint, unlocked/orange boot state, disabled
   AVB verification and verity, active slot, battery, and empty Telecom state;
3. requires the image size to exactly equal the active logical system
   partition and revalidates its filesystem and embedded files;
4. writes a receipt containing image and rollback SHA-256 hashes;
5. enters userspace fastbootd and confirms the target is logical;
6. flashes only `system_a` (or the detected active system slot), never `super`,
   bootloader, modem, vendor, or user data;
7. boots Android, removes only PhoneAgent's recoverable update, requires the
   flashed system path, and performs one-time user-0/runtime/dialer/key
   provisioning;
8. verifies the system path, link-key hash, and effective privileged grants.

The currently deployed authenticated proof image has SHA-256
`56427c6c213ddec8b4e657198246bf4c8c16e37969248b2966177c88768d6290`.
The untouched rollback image has SHA-256
`0c3276a30b0a45b9b87eb629d50d90fb1dafbbb8e556f0b5dd60dcc4ef22d42d`.
The flash receipt is stored under `artifacts/persistent-gsi/`.

After this one-time persistent deployment, ordinary reboots require no
installer. `BootReceiver` starts the phone-side services automatically.
The Mac must recreate ADB forwards after a USB/ADB reconnection; this is host
connection state, not phone installation state, and `FramedGatewayLink` does it
automatically.

To roll back the system image deliberately:

```bash
adb -s rgr8r8zxmv9txgi7 reboot fastboot
fastboot -s rgr8r8zxmv9txgi7 getvar is-userspace
fastboot -s rgr8r8zxmv9txgi7 flash system_a \
  /Users/aziz/Documents/PhoneAgent/scratch/gsi/system.img
fastboot -s rgr8r8zxmv9txgi7 reboot
```

Rollback removes the built-in PhoneAgent package. It does not erase user data,
vendor, modem, boot, or the other survival backups.

### 14.5 Verify the installed gateway

```bash
adb shell pm path com.phoneagent.gateway
adb shell cmd role get-role-holders android.app.role.DIALER 0

adb shell dumpsys package com.phoneagent.gateway \
  | rg 'CAPTURE_AUDIO_OUTPUT|MODIFY_AUDIO_ROUTING|MODIFY_PHONE_STATE|CONTROL_INCALL_EXPERIENCE'
```

Expected results include the system `priv-app` path, the PhoneAgent package as
the dialer holder, and `granted=true` for each reviewed privileged permission.

Start the service and create the USB port mappings:

```bash
adb shell am start-foreground-service \
  -n com.phoneagent.gateway/.GatewayService

adb forward tcp:8765 tcp:8765
adb forward tcp:8766 tcp:8766
adb forward tcp:8767 tcp:8767
adb forward tcp:8768 tcp:8768
```

Check readiness from the Mac:

```bash
curl -fsS http://127.0.0.1:8765/health | python3 -m json.tool
python3 mac_client/audio_probe.py status
```

A ready response must show `gateway: ready`, `dialer_role: true`, `state:
IDLE`, the capture permission, and both audio server ports. A reported
Telephony TX device is only a capability signal; the real-call tests below are
the physical proof.

### 14.6 Call-control API

All services bind to phone loopback. Port `8765` remains useful for read-only
`GET /health`, `GET /call/status`, and `GET /audio/status` diagnostics. Once a
link key is provisioned, mutating HTTP endpoints intentionally return HTTP 426
and do not change phone state. Production mutations use authenticated PHAG v1
commands on port `8768`:

| PHAG command | Purpose |
|---|---|
| `gateway.health` | Combined call, role, service, and audio health |
| `call.status` | Current Telecom state and remote number |
| `call.dial` | Place a non-emergency outgoing call |
| `call.answer` / `call.reject` | Accept or reject a ringing call |
| `call.hangup` | Disconnect the tracked call |
| `dtmf.send` | Send one validated DTMF digit |
| `audio.status` | Routes, counters, generation, and errors |
| `audio.flush` | Urgently advance generation and discard queued output |

Every command carries a UUID `command_id`; Android caches the last 256 command
results so a reconnect/retry cannot repeat the telephony mutation. Every frame
is bound to a link epoch and call ID and authenticated with HMAC-SHA256.

Example manual flow using a consenting, non-emergency test destination:

```python
from pathlib import Path

from phone_agent_gateway.ai_bridge.session import CallSessionState, SessionPhase
from phone_agent_gateway.mac_client.framed_link import load_link_key
from phone_agent_gateway.mac_client.gateway_client import CallState
from phone_agent_gateway.mac_client.protocol_client import (
    AuthenticatedPhoneAgentClient,
    wait_for_state,
)

session = CallSessionState()
session.set_phase(SessionPhase.CONNECTING)
client = AuthenticatedPhoneAgentClient(
    session,
    load_link_key(str(Path.home() / ".config/phone-agent/link.key")),
    device_id="ADB_DEVICE_SERIAL",
)
try:
    client.connect_control()  # also establishes/refreshes the ADB forwards
    print(client.get_health())
    result = client.dial("CONSENTING_TEST_NUMBER")
    if result.get("status") != "ok":
        raise RuntimeError(result)

    wait_for_state(client, {CallState.ACTIVE}, timeout=45.0)
    session.set_phase(SessionPhase.ACTIVE)
    client.connect_media()
    # Start PhoneAgentTransport/PipelineWorker only after ACTIVE + media ready.
finally:
    try:
        if client.get_status().state not in {CallState.IDLE, CallState.DISCONNECTED}:
            client.hangup()
    except Exception:
        pass
    client.close()
```

`CallManager` refuses emergency numbers, validates DTMF, and prevents a second
concurrent call. The SDK creates command IDs, verifies acknowledgements, and
never exposes the authentication key in logs. Its lower-level `request` method
accepts an explicit command UUID when a caller must retry the exact operation.

### 14.7 Digital media contract

The production socket contract is authenticated PHAG v1 framing:

| Direction | Port | Wire payload | Meaning |
|---|---:|---|---|
| Phone -> Mac | `8766` | PHAG v1 audio frame containing PCM16/16 kHz/mono | Remote cellular downlink |
| Mac -> Phone | `8767` | PHAG v1 audio frame containing PCM16/16 kHz/mono | Audio injected into modem uplink |
| Bidirectional | `8768` | PHAG v1 command/ACK/error JSON frames | Control and urgent flush |

Each media frame carries version, direction, call ID, link epoch, generation,
sequence, monotonic timestamp, audio format, payload length, and HMAC. Android
requires an authenticated channel handshake, exact payload bounds, increasing
sequences, the active call/epoch, and the current generation before accepting
audio. Never write naked PCM to ports `8766` or `8767`.

One 20 ms frame is 320 samples or 640 bytes:

```python
SAMPLE_RATE = 16_000
FRAME_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000  # 320
BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2             # 640
```

The Android bridge upsamples injected 16 kHz PCM once to the verified 48 kHz
Telephony TX profile. Do not resample the same audio at several layers.
`PhoneAgentOutputTransport` paces Pipecat output in real time and delegates the
identity and HMAC envelope to `FramedGatewayLink`:

```python
transport = PhoneAgentTransport(params, session=session)
client.link.on_audio_received(transport.feed_phone_frame)
transport.set_tx_handler(client.link.send_audio_chunk)
transport.set_flush_handler(client.flush_audio)
```

The input transport validates framed identity again, owns a bounded queue, and
drops oldest audio rather than allowing latency to grow. The output transport
obtains generation/sequence identity from `CallSessionState` immediately before
each send and drops a frame if interruption advanced the generation during
pacing. The real Pipecat probe in Section 13 proves the complete transaction.

### 14.8 Interruption and generation flush

When VAD detects caller speech while TTS is playing, perform these operations
in order:

```python
# 1. Stop the Mac producer and cancel current TTS/LLM work.
tts_cancel_event.set()

# 2. Queue Pipecat's worker-safe interruption signal.
await worker.queue_frame(InterruptionWorkerFrame())

# 3. PhoneAgentOutputTransport advances local generation and invokes the
# authenticated urgent audio.flush command. Android synchronizes generation,
# pauses/flushes/restarts AudioTrack, and rejects late old-generation frames.
```

The current wiring is:

```python
client.link.on_audio_received(transport.feed_phone_frame)
transport.set_tx_handler(client.link.send_audio_chunk)
transport.set_flush_handler(client.flush_audio)
```

The original direct GSM feasibility test measured 101 ms and the later direct
PHAG probe measured 29.8 ms. The real Pipecat worker test measured 38.46 ms from
detected caller onset to Android flush acknowledgement and proved rejection of
a deliberately late old-generation frame.

### 14.9 Repeat the proven hardware tests safely

These commands mutate real telephony state, call a person, and may record their
voice. Use only a consenting test participant. Never use emergency numbers.

Current authenticated Pipecat transport/interruption proof: the participant
answers, waits for the low-level tone, then speaks over it.

```bash
.venv/bin/python -m phone_agent_gateway.mac_client.pipecat_call_probe \
  CONSENTING_TEST_NUMBER \
  --device-id ADB_DEVICE_SERIAL \
  --confirm-call \
  --confirm-tone
```

The probe creates a real `PipelineWorker`, attaches PHAG v1 media only after
`ACTIVE`, records caller frames, emits paced output, triggers a standard worker
interruption, verifies the Android generation acknowledgement, sends one
deliberately stale frame, requires Android's rejection counter to increase,
and always attempts worker shutdown and hang-up. It preserves JSON and WAV
evidence under `artifacts/pipecat-calls/`.

The older commands below remain useful only for raw feasibility diagnostics,
not as the production protocol path.

Caller-only downlink capture:

```bash
python3 mac_client/live_call_test.py \
  --number CONSENTING_TEST_NUMBER \
  --output artifacts/downlink-proof.wav \
  --seconds 20 \
  --confirm-dial
```

Digital uplink injection, with an expected low-level tone:

```bash
python3 mac_client/live_call_test.py \
  --number CONSENTING_TEST_NUMBER \
  --output artifacts/uplink-proof.wav \
  --seconds 20 \
  --inject-tone \
  --tone-delay 4 \
  --tone-seconds 1 \
  --tone-frequency 1000 \
  --tone-amplitude 0.05 \
  --confirm-dial \
  --confirm-tone
```

Full-duplex barge-in test: the participant remains quiet until the beep starts,
then speaks over it.

```bash
python3 mac_client/live_call_test.py \
  --number CONSENTING_TEST_NUMBER \
  --output artifacts/barge-in-proof.wav \
  --seconds 12 \
  --inject-tone \
  --tone-delay 4 \
  --tone-seconds 3 \
  --tone-amplitude 0.05 \
  --barge-in \
  --barge-threshold-dbfs -42 \
  --barge-frames 2 \
  --post-barge-seconds 3 \
  --confirm-dial \
  --confirm-tone
```

The runner owns dial, `ACTIVE` detection, capture attachment, injection,
disconnect monitoring, WAV finalization, and hang-up cleanup in one process.
This avoids the timing race created by separate dial and capture commands.

### 14.10 Incoming-call gateway loop

The implemented `phone-agent-voice` host polls call state through the
authenticated client. Under explicit `PHONE_AGENT_AUTO_ANSWER=true` policy it
answers `RINGING`; on `ACTIVE` it attaches framed media, activates the session,
starts exactly one `ProductionCallPipeline`, and greets only after the worker is
ready. On `IDLE`/`DISCONNECTED` it stops the worker and replaces the isolated
session. Link failure uses bounded exponential reconnection.

```bash
export PHONE_AGENT_LINK_KEY_FILE="$HOME/.config/phone-agent/link.key"
export PHONE_AGENT_DEVICE_ID="ADB_DEVICE_SERIAL"
export PHONE_AGENT_AUTO_ANSWER=false
# Configure the selected STT/LLM/TTS credentials before starting.
.venv/bin/phone-agent-voice
```

Do not automatically answer every public call in production. Add caller policy,
rate limits, consent/recording disclosure, operating hours, and a safe fallback
when the Mac or AI providers are unavailable.

### 14.11 Troubleshooting

| Symptom | Checks and likely action |
|---|---|
| `connection refused` on 8765 | Recreate ADB forwards, start `GatewayService`, then inspect `adb logcat` |
| HTTP mutation returns 426 | Expected after link-key provisioning; use `AuthenticatedPhoneAgentClient` on port 8768 |
| Authenticated dial is rejected | Check for an existing call, dialer role, invalid/missing number, emergency refusal, and the structured error ACK |
| Call UI appears but gateway stays `IDLE` | Verify `ROLE_DIALER` and that Telecom bound `PhoneAgentInCallService` |
| RX connects but WAV is silent | Verify `CAPTURE_AUDIO_OUTPUT`, `capture_source`, active GSM/IMS route, and `downlink_bytes` |
| TX bytes increase but remote hears nothing | Treat route as unproven; inspect Telephony TX device, Audio Policy, HAL, and vendor mixer path |
| AI audio continues after interruption | Stop producer first, send urgent authenticated `audio.flush`, verify generation advanced, and inspect bounded output queues |
| `last_error` remains populated | Inspect `PhoneAgentDigitalAudio` logcat entries; do not hide unexpected HAL or socket faults |
| App is no longer privileged after reboot | Verify the booted slot and persistent-image hash; restore the persistent GSI with `flash_persistent_gsi.sh`, or flash the pristine rollback image intentionally |

Useful diagnostics:

```bash
adb logcat -v time \
  | rg 'PhoneAgent(CallManager|DigitalAudio|HttpServer|Gateway)'

adb shell dumpsys telecom
adb shell dumpsys audio
curl -fsS http://127.0.0.1:8765/audio/status | python3 -m json.tool
```

### 14.12 Security and operational constraints

- Keep ports 8765-8768 bound to Android loopback and expose them only through
  the trusted ADB session. PHAG authentication is defense in depth, not a reason
  to expose telephony control publicly.
- Do not bind the gateway to `0.0.0.0`, Wi-Fi, or a public tunnel.
- Never automate emergency calls.
- Require explicit authorization for live diagnostic calls, recording, and
  injected test audio.
- Store recordings as sensitive personal data and define retention/deletion
  rules before production.
- Refuse a new session when another call is tracked.
- On Mac disconnect, cancel generation, flush output, close media sockets, and
  apply the configured hang-up or local fallback policy.
- Revalidate audio gates separately for GSM, VoLTE/IMS, Wi-Fi calling, another
  SIM/carrier, ROM update, or different phone model. The current proof applies
  to the tested Redmi/ROM/GSM route only.

## 15. Mac conversational intelligence runtime — end-to-end specification

This section converts the framework research into the production design. The
goal is not a scripted voice bot. The target is a broad conversational system
that listens continuously, understands open-ended speech, reasons and uses
tools, speaks naturally, and yields immediately when the caller interrupts.

### 15.1 Architectural decision

Use **Pipecat 1.x** as the production conversation orchestration framework.
Build a project-owned `PhoneAgentTransport` that connects Pipecat directly to
the authenticated, framed Android gateway protocol. Keep all speech, language,
voice, and tool providers behind replaceable interfaces.

The decision is based on the following project-specific requirements:

- direct PCM input/output without a telephony SaaS or SIP requirement;
- custom call ID, sequence, timestamp, and generation framing;
- urgent full-pipeline cancellation and Android/HAL flush;
- semantic turn detection plus local VAD;
- support for cascaded STT/LLM/TTS and native realtime speech models;
- Python integration with the existing Mac clients;
- per-call isolation, tools, observers, metrics, and tests;
- an active permissively licensed upstream.

Pipecat is the **runtime**, not the intelligence itself. Conversation quality
still depends on model selection, context design, memory, tools, audio quality,
and objective evaluation.

### 15.2 Framework research record

Research snapshot: 2026-08-25. Repository popularity is informative but is not
an acceptance criterion; direct architectural fit, license, activity, and test
evidence take priority.

| Project | License/status | Relevant strengths | PhoneAgent decision |
|---|---|---|---|
| [Pipecat](https://github.com/pipecat-ai/pipecat) | BSD-2-Clause; active 1.x releases | Ordered audio/control frames, custom input/output transports, interruption propagation, Smart Turn, provider plugins, metrics | **Selected production runtime** |
| [LiveKit Agents](https://github.com/livekit/agents) | Apache-2.0; active 1.x releases | Mature `AgentSession`, audio turn detector, adaptive interruption, false-interruption recovery, dynamic endpointing, preemptive generation, tools and workflows | Strong reference and future alternative if the system adopts LiveKit rooms/WebRTC/SIP |
| [TEN Framework](https://github.com/TEN-framework/ten-framework) | Repository license adds conditions beyond Apache-2.0 | High-performance extension graph, typed audio frames, cascade and realtime V2V patterns | Not selected: heavier than needed and [license restrictions](https://github.com/TEN-framework/ten-framework/blob/main/LICENSE) require legal review |
| [FastRTC](https://github.com/gradio-app/fastrtc) | MIT | Fast local/WebRTC real-time audio prototypes | Prototype utility, not the complete call-agent runtime |
| [Bolna](https://github.com/bolna-ai/bolna) | MIT; active | Packaged conversational voice-agent integrations | More opinionated around existing provider/telephony paths than the direct rooted-phone boundary |
| [Vocode](https://github.com/vocodedev/vocode-core) | MIT | Clear modular voice-agent concepts | Not selected for a new build because core development/release activity trails the leaders |

LiveKit's turn-taking design remains useful reference material, especially its
adaptive interruption, false-interruption recovery, dynamic endpointing, and
preemptive-generation controls. Its standard media path uses LiveKit rooms and
`RoomIO`; bridging the already verified USB PCM into a room would add another
media layer. Re-evaluate LiveKit only if multi-party WebRTC, distributed media
servers, or SIP become primary requirements.

### 15.3 End-to-end runtime topology

```text
Cellular caller
    |
    | GSM / VoLTE voice
    v
Rooted Android PhoneAgent
    |  caller-only downlink: PCM16 / 16 kHz / mono / 20 ms
    |  control: call state, acks, health, errors
    v
Authenticated PhoneAgent protocol on Mac
    |
    +--> Reflex loop ---------------------------------------------+
    |    VAD -> interruption candidate -> local mute/flush        |
    |                                                             |
    +--> Pipecat input transport                                  |
         -> frame/call/generation validator                       |
         -> speech understanding + semantic turn detector         |
         -> context and conversation coordinator                  |
              |                                                   |
              +--> fast ordinary-response route                   |
              +--> deep reasoning / retrieval / tools             |
              +--> native duplex experiment route                 |
         -> one authorized response stream                        |
         -> streaming TTS or native speech output                 |
         -> generation filter                                     |
         -> Pipecat output transport                              |
    |                                                             |
    +<------------------------------------------------------------+
    |  generation-aware PCM16 / 16 kHz / mono / 20 ms
    v
Android Telephony TX -> carrier -> caller
```

The phone is the cellular ears and mouth. The Mac is the conversational brain.
No model, database, or business API may run in the transport receive/write
tasks.

### 15.4 The three coordinated loops

#### Reflex loop

Purpose: protect conversational timing without waiting for transcription or a
remote model.

Responsibilities:

- inspect every 20 ms caller frame with a lightweight local VAD;
- detect likely caller speech while the assistant is speaking;
- stop the Mac output producer immediately;
- advance the output generation;
- clear interruptible Pipecat and local transport queues;
- send urgent `audio.flush` to Android;
- record onset-to-local-mute and onset-to-`flush_ack` latency;
- later accept correction from the semantic classifier if the event was only
  noise or a backchannel.

This loop must remain functional when STT, LLM, TTS, retrieval, and tools are
slow or unavailable.

#### Conversation loop

Purpose: handle ordinary natural dialogue at very low perceived latency.

Responsibilities:

- continuously stream audio rather than wait for a recording;
- consume interim and final recognition or direct speech-model events;
- combine VAD, transcript, acoustic, and semantic turn evidence;
- begin safe speculative reasoning before final turn commitment;
- stream response text and TTS incrementally;
- update context with what the caller actually heard, not unsent text;
- handle corrections, short acknowledgements, hesitation, and code-switching.

#### Deliberation loop

Purpose: perform work that cannot finish inside the immediate response budget.

Responsibilities:

- deep reasoning and planning;
- retrieval from approved knowledge sources;
- CRM, calendar, order, messaging, database, and other tools;
- deadlines, retries, idempotency, confirmation, and compensation;
- return a typed result to the coordinator.

When work will take noticeable time, the coordinator may issue one short,
truthful acknowledgement such as “Let me check that.” The tool loop itself is
never allowed to speak directly.

### 15.5 One-mouth conversation coordinator

The central invariant is **one call, one coordinator, one mouth**.

The coordinator owns:

- current call, user turn, assistant turn, and generation IDs;
- which pipeline/backend is authoritative for the turn;
- whether a partial transcript is stable enough for speculation;
- whether the caller paused, finished, interrupted, backchanneled, or produced
  unrelated background speech;
- whether to answer quickly, invoke deeper reasoning, use a tool, clarify,
  refuse, or escalate;
- the exact text/audio prefix released to the caller;
- cancellation and context repair after interruption;
- language, persona, voice, privacy, and recording policy.

No fast model, deep model, TTS service, tool callback, or native duplex model
may bypass the coordinator and write directly to the phone.

A simplified turn state machine is:

```text
LISTENING
  -> USER_SPEAKING
  -> TURN_CANDIDATE_END
  -> SPECULATING
  -> TURN_COMMITTED
  -> THINKING / TOOL_WAIT
  -> SPEAKING
  -> INTERRUPTED -> LISTENING
  -> COMPLETE -> LISTENING

any state -> CANCELLING -> SAFE_FALLBACK / LISTENING / END_CALL
```

### 15.6 Real PhoneAgent Pipecat transport

The current `ai_bridge/pipecat_transport.py` contains bounded queues and useful
callback concepts, but it is not a Pipecat transport implementation. Replace
it with the following logical components:

```text
PhoneAgentTransport
  PhoneAgentInputTransport   <- Pipecat BaseInputTransport
  PhoneAgentOutputTransport  <- Pipecat BaseOutputTransport
  PhoneAgentControlAdapter   <- call/control events and urgent flush
  FrameCodec                 <- versioned binary encode/decode
  SessionAuthenticator       <- pinned key or mutual TLS
  LinkSupervisor             <- ADB forwarding, reconnect, health, epochs
```

#### Input responsibilities

- receive complete protocol frames, not arbitrary TCP read boundaries;
- validate version, direction, call ID, generation, sequence, timestamp,
  format, length, and authentication state;
- reject oversized, duplicated, replayed, stale, or wrong-call frames;
- convert accepted caller PCM into Pipecat `InputAudioRawFrame` objects;
- push frames without executing model, database, or tool work;
- keep the queue bounded and report every dropped sequence;
- never copy assistant uplink audio into the caller-only input stream.

Illustrative structure; pin imports to the chosen Pipecat release during
implementation:

```python
class PhoneAgentInputTransport(BaseInputTransport):
    async def on_phone_audio(self, packet: AudioPacket) -> None:
        session.validate_input(packet)
        await self.push_audio_frame(
            InputAudioRawFrame(
                audio=packet.payload,
                sample_rate=packet.sample_rate,
                num_channels=packet.channels,
            )
        )
```

#### Output responsibilities

- accept Pipecat output audio frames;
- resample exactly once to the 16 kHz/mono gateway format if required;
- split into paced 20 ms/640-byte frames;
- attach call ID, generation, sequence, and intended-playback timestamp;
- enforce bounded queue age before sending;
- reject output if the call is not `ACTIVE` or the generation is stale;
- map Pipecat interruption immediately to local queue reset plus Android flush;
- record bytes accepted, sent, acknowledged, dropped, and rendered.

```python
class PhoneAgentOutputTransport(BaseOutputTransport):
    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        pcm16k = await resample_once(frame)
        for chunk in split_20_ms(pcm16k):
            await media.send_audio(
                call_id=session.call_id,
                generation_id=session.generation_id,
                sequence=session.next_output_sequence(),
                pcm=chunk,
            )
        return True

    async def process_frame(self, frame, direction):
        if isinstance(frame, InterruptionFrame):
            await session.interrupt_and_flush_phone()
        await super().process_frame(frame, direction)
```

The production implementation must confirm the exact Pipecat superclass call
order so the framework clears its queues and the Android flush occurs without
losing urgent control propagation.

### 15.7 Generation and interruption transaction

Treat interruption as one measured distributed transaction:

```text
1. Local VAD marks caller speech onset T0.
2. Coordinator marks generation G cancelled.
3. Stop accepting new TTS/model output for G.
4. Emit Pipecat InterruptionFrame.
5. Clear interruptible Mac output queues for G.
6. Increment local generation to G+1.
7. Send urgent audio.flush(call_id, cancelled=G, next=G+1).
8. Android stops, flushes, and restarts its AudioTrack safely.
9. Android returns flush_ack with last accepted/rendered sequence.
10. Mac rejects every later packet or callback belonging to G.
11. Context stores only the assistant prefix proven or estimated as heard.
```

The control channel must remain independent of media backpressure, so step 7
cannot sit behind queued audio. Reconnect does not resurrect a cancelled
generation. A new link epoch requires explicit session resynchronization.

### 15.8 Production intelligence path: streaming cascade

Use the cascade as the first production baseline:

```text
caller PCM
 -> local VAD
 -> streaming STT with interim/final text and timing
 -> semantic/adaptive end-of-turn
 -> context + memory retrieval
 -> fast powerful streaming LLM
 -> tool/reasoning route when required
 -> streaming TTS
 -> generation filter
 -> phone PCM
```

Why this remains the production baseline:

- a strong general LLM currently gives the broadest reasoning and knowledge;
- explicit text makes tool arguments, auditing, safety, and debugging easier;
- STT, LLM, and TTS can be benchmarked and replaced independently;
- transcripts support memory and objective entity/error evaluation;
- business operations can be confirmed deterministically.

Provider rules:

- open STT, LLM, and TTS sessions during `CONNECTING`/`ANSWERING`;
- use persistent streaming connections, not one request per utterance;
- consume interim transcripts but do not commit unstable text to memory;
- begin speculative LLM generation only when it is cancellable;
- do not release speculative TTS until the coordinator's safety policy allows;
- stream TTS at phrase/chunk granularity and preserve pronunciation metadata;
- assign every provider request the call, turn, and generation IDs;
- enforce short deadlines and a fallback provider or cached critical phrase;
- never allow a provider retry to duplicate a tool action or spoken response.

#### Local Apple-Silicon baseline checkpoint — 2026-08-25

The project now also supports a credential-free private baseline selected with
`PHONE_AGENT_STT_PROVIDER=whisper_mlx`, `PHONE_AGENT_LLM_PROVIDER=ollama`, and
`PHONE_AGENT_TTS_PROVIDER=kokoro`. It uses Pipecat 1.7's maintained segmented
MLX Whisper adapter, the then-current local Ollama `qwen3.8:latest`, and
streaming Kokoro ONNX.
Provider validation requires no cloud keys for this combination, while the
existing Deepgram Flux/Cartesia path retains its credential requirements.

The content-free offline preflight loaded all three real providers, generated a
greeting, fed that synthesized speech back through STT, completed a second LLM
and TTS turn, produced 273 exact 20 ms output frames, and recorded two assistant
context messages. Evidence is at
`artifacts/provider-preflight/local-cascade.json`. This proves functional local
composition, not acceptable conversational latency: the cold provider load was
112.37 seconds, greeting-first-audio was 34.41 seconds, and the synthetic
loopback response began after 76.24 seconds. Models must remain prewarmed and be
benchmarked warm; this cold configuration is retained only as historical
evidence and must not be selected for another natural call.

#### Ollama versus Antigravity live latency benchmark — 2026-08-25

The live comparison used the same deterministic prompts, alternated provider
order, required exact output, and measured usable response text rather than
connection establishment. The historical tested models were local
`qwen3.8:latest` and
Antigravity `gemini-3.7-flash-control`.

With the current Pipecat-style Ollama OpenAI endpoint, the local model emitted
hidden reasoning before answer text. Across 13 warm paired trials, Ollama's
median first usable text was 9.543 seconds and median completion was 11.488
seconds. Antigravity's unary response reached both first usable text and
completion at a 1.413-second median. Both providers returned the exact requested
output in all 13 trials. Cold invocation separately took 18.625 seconds for the
unloaded 17 GB Ollama model and 1.755 seconds for Antigravity.

This default result is not the local model's latency floor. Eight additional
paired telephone-sentence trials used Ollama's native streaming `/api/chat`
with `think=false` while keeping the model resident. Ollama then measured:

- first text: 396.8 ms median, 550.9 ms P95;
- first 20 response characters: 1,063.9 ms median, 1,362.2 ms P95;
- full response: 2,659.4 ms median, 2,823.7 ms P95.

Antigravity measured 1,445.9 ms median and 1,742.8 ms P95 for first text, first
20 characters, and completion because its upstream RPC is unary and releases
the complete answer as one chunk. Exact-output rate was 100% for both providers.

Therefore the prewarmed, no-thinking local Ollama path is the measured winner
for first-token and first-speakable-phrase latency, which are the important
metrics for streamed telephone TTS. Antigravity completes these short responses
faster, and starts much faster from a cold state, but cannot progressively feed
TTS. The production path now uses a dedicated native adapter rather than the
OpenAI-compatible Ollama service: it calls `/api/chat`, sends `think=false`,
streams bounded NDJSON content directly into Pipecat, encodes `keep_alive=-1`
as a number, prewarms with the exact 8,192-token context, and closes the active
HTTP response on interruption. The later `qwen3.5:4b-mlx` promotion below
supersedes the instruction to keep this 17 GB runner resident. This benchmark
measures speed and exact instruction adherence only, not general conversational
understanding.

#### Native Ollama and first natural call checkpoint — 2026-08-25

The native service is implemented in `ai_bridge/ollama_native.py` and is now
the factory output for `PHONE_AGENT_LLM_PROVIDER=ollama`. Unit tests cover URL
normalization, exact prewarm wire requests, incremental NDJSON content/usage,
cancellation, context conversion, and traversal through a real Pipecat worker.
A live long-generation cancellation closed in 3.6 ms and a follow-up request
completed normally, proving cancellation does not poison the resident runner.

The first real natural cellular call then completed four assistant turns using
MLX Whisper, native Ollama, and Kokoro. Generation advanced from 1 to 5 under
caller interruptions. Content-free evidence is at
`artifacts/natural-calls/native-ollama-kokoro-20260825-140159.json`. The four
user-to-bot measurements were 7,153, 4,450, 4,246, and 8,129 ms (5,801 ms
median). This proves the intelligent call path and context lifecycle, but it
does not pass the latency or audible-quality gate.

#### TTS quality recovery and Edge checkpoint — 2026-08-25

The Kokoro experiment exposed a provider-integration error as well as a model
quality limitation. Token mode sent fragments too small for natural synthesis,
stretched a short greeting to 5.38 seconds, and passed a standalone `-` token
that produced no phonemes. Local Kokoro now defaults to sentence aggregation;
it remains an offline fallback, not the selected high-quality voice.

`edge_tts` 7.2.8 is implemented as an experimental high-quality provider. The
adapter accepts only speakable phrases, streams Microsoft's 24 kHz mono MP3,
feeds every network chunk through one cancellable FFmpeg process, and emits
continuous mono PCM16 at 16 kHz. It never restarts the decoder at MP3 network
boundaries. A bounded phrase aggregator releases at natural punctuation or a
configured 72-character limit, avoiding both token cuts and full-response
waiting. Interruption cancels the network writer and decoder process.

Five real direct trials from this Mac using
`en-US-EmmaMultilingualNeural` succeeded without errors. Median first PCM was
544.75 ms and median complete synthesis was 689.35 ms for 4.416 seconds of
audio, proving faster-than-real-time output. Evidence is at
`artifacts/provider-preflight/edge-tts-direct-20260825.json`. In the complete
MLX-Whisper/native-Ollama/Edge preflight, phrase aggregation reduced greeting
first audio from 17.028 seconds under sentence waiting to 4.111 seconds and
preserved exact 20 ms phone frames. The synthetic second response still began
at 10.330 seconds, so endpointing and LLM phrase latency remain active work.

A controlled cellular Edge call completed one caller/assistant turn with a
5.723-second user-to-bot measurement: MLX STT first result 1.459 seconds, safe
text aggregation 2.078 seconds, and Edge first PCM 580 ms. Evidence is at
`artifacts/natural-calls/native-ollama-edge-tts-20260825-142557.json`. Edge is
online and uses an unofficial browser speech endpoint, so it must retain a
local fallback and may not be promoted to unattended production until repeated
calls establish quality and reliability. The supported Azure or Cartesia path
remains the production fallback candidate if Edge availability is unstable.

#### `qwen3.5:4b-mlx` production promotion — 2026-08-25

The installed `qwen3.5:4b-mlx` runner is now the default for both
`PHONE_AGENT_LLM_MODEL` and the Ollama provider. Local inspection verified the
4.0 GB installed model and found its packaged defaults were temperature `1`,
top-p `0.95`, top-k `20`, min-p `0`, and presence penalty `1.5`. The previous
adapter did already override temperature with `0.0`; it did not inherit the
packaged temperature of `1`. It did, however, omit the other sampling controls.

Every prewarm and conversational request now carries the same complete voice
configuration: temperature `0.7`, top-p `0.8`, top-k `20`, min-p `0`, presence
penalty `0`, 192 predicted tokens, an 8,192-token context, `think=false`, and
numeric `keep_alive=-1`. All values are environment-configurable. The system
prompt also asks for one short, immediately speakable opening sentence without
Markdown, while retaining confirmation and uncertainty safeguards. Prewarm
runs while the host starts and before the gateway reports ready, so a call does
not trigger first-use loading.

Eight native streaming telephone-reply trials used this exact configuration.
The first trial saw a 3.223-second runner-transition outlier. Across the seven
subsequent steady-state trials, first text was 57--268 ms and complete short
replies were 299--404 ms. Across all eight trials, median first text was 72.92
ms, median first 20 characters was 140.18 ms, median completion was 357.96 ms,
and median wall throughput was 32.13 tokens/s. Every reply passed the fixed
one-sentence, at-most-14-word constraint. Content and timing evidence is at
`artifacts/provider-preflight/qwen3.5-4b-mlx-native-warm-20260825.json`.

The complete MLX-Whisper -> `qwen3.5:4b-mlx` -> Edge phrase preflight preserved
exact 20 ms audio frames. Greeting-first-audio improved from the former
`qwen3.8:latest` result of 4.111 seconds to 1.674 seconds, a 59.3% reduction.
Synthetic loopback response-first-audio improved from 10.330 seconds to 7.612
seconds, a 26.3% reduction. Evidence is at
`artifacts/provider-preflight/qwen3.5-4b-mlx-edge-tts-phrase.json`. The large
remaining loopback time confirms that speech endpointing/STT and phrase release,
not steady-state LLM generation alone, are now the primary latency work.

The former `qwen3.8:latest` runner was unloaded with `ollama stop` but was not
deleted. `qwen3.5:4b-mlx` remains GPU-resident with an indefinite keep-alive.
Tool calling is supported by the model, but gateway business-tool schemas,
confirmation loops, idempotency, and evaluations remain Work Package E rather
than being claimed complete by this model switch.

#### Bounded Mac latency D1 checkpoint — 2026-08-25

The phone/APK path is unchanged and frozen. Inspection of the installed
Pipecat 1.7 implementation confirmed that Deepgram Flux already provides the
authoritative `StartOfTurn`, `EagerEndOfTurn`, `TurnResumed`, and `EndOfTurn`
lifecycle. Final EOT emits a finalized transcript and immediately triggers the
normal LLM path. Eager EOT emits only an interim transcript; Pipecat explicitly
does not yet provide a safe cancellable eager-output gate.

The Mac now binds a content-free `FluxTurnTimingTracker` to the public Flux
events. It maintains `LISTENING`, `EAGER`, `RESUMED`, and `COMMITTED` state plus
a monotonic transcript revision number. Telemetry records character counts,
partial revision count, start-to-first-update, start-to-final, eager-to-final,
and whether normalized eager/final text agrees. Transcript text and hashes are
not stored in telemetry.

Two small correctness/latency fixes accompany the tracker:

- Flux uses `ExternalUserTurnStrategies` from pipeline construction, avoiding
  loading local Smart Turn and running Silero on a path where Flux is already
  authoritative. The Whisper fallback retains its existing local VAD/turn
  behavior.
- Flux `min_confidence` is `0.0`. A completed turn is preserved when confidence
  metadata is missing or low instead of silently dropping its transcript and
  leaving the caller waiting for a turn timeout. Critical uncertainty must be
  handled through clarification, not deletion.

This is intentionally measurement-first. No second Ollama request, speculative
TTS context, PCM commit buffer, new provider, or framework was added. A safe
speculative path is justified only if live Flux traces show useful reusable
lead and acceptable eager/final agreement. No Deepgram key is configured in
the current shell, so live Flux timing remains the next credential-dependent
measurement rather than being claimed complete.

#### Invalid destination-reachability trial — 2026-08-25 15:37

An outbound trial through the unchanged phone gateway used the credential-free
Mac stack: MLX Whisper -> native Ollama `qwen3.5:4b-mlx` -> Edge TTS. Android
correctly normalized the requested Moroccan international number to its local
equivalent and Telecom progressed `IDLE -> DIALING -> ACTIVE -> IDLE`.
However, the intended handset did not ring. The carrier marked the call active
after only 3.48 seconds and later disconnected it with remote telephony cause
65, so the captured speech was likely a carrier announcement or immediate
forwarding destination. This trial is not a successful human conversation and
must not count toward either the three-call Flux target or a quality benchmark.

The pipeline response to that non-human audio measured 2,823 ms to first audio
(876 ms Whisper TTFB, 136 ms phrase aggregation, 1,576 ms Edge TTS TTFB, and
approximately 235 ms elsewhere). These numbers remain useful only as a rough
Mac-path diagnostic; they do not establish caller-perceived production latency.
The number, transcript, and audio were not persisted by the project.

#### Lightning Whisper MLX assessment — 2026-08-25

[`lightning-whisper-mlx`](https://github.com/mustafaaljadery/lightning-whisper-mlx)
is approved only as an isolated benchmark candidate, not a production
dependency:

- its advertised advantage comes from batched decoding, distilled models, and
  quantization; that is attractive for long recordings/throughput but must be
  remeasured on 1–8 second telephone turns where batching may offer little;
- its public API transcribes a completed path/array rather than maintaining a
  partial-hypothesis streaming session, and the repository's real-time-input
  request remains open;
- the main branch's last merged commit is from 2024-05-08, PyPI remains 0.0.10
  from 2024-04-02, and turbo support is still an open request/unmerged PR;
- an open issue reports missing audio at the end of 20–30 second inputs, and
  another discussion recommends external Silero VAD to limit hallucinations;
- the repository and PyPI metadata declare no license. Do not copy, modify, or
  distribute it in the production gateway without an explicit license grant;
- third-party realtime integrations have reported that uncoordinated concurrent
  MLX STT/TTS inference can crash Metal. Any benchmark must serialize MLX access
  and measure the latency penalty.

Benchmark it in an isolated environment against Pipecat's maintained
`WhisperSTTServiceMLX` using the same fixed cellular corpus. Compare warm model
load, short-turn P50/P95 transcription latency, multilingual/code-switching
accuracy, end truncation, hallucination rate, memory, and interruption behavior.
Promote it only if licensing is resolved, true turn latency improves materially,
and every correctness/reliability gate passes.

### 15.9 Native full-duplex model track

Maintain a common backend interface for models that listen and speak
simultaneously:

```python
class DuplexSpeechBackend(Protocol):
    async def start(self, session_context): ...
    async def push_audio(self, pcm: bytes, timestamp_ms: int): ...
    async def output_audio(self) -> AsyncIterator[DuplexAudioChunk]: ...
    async def interrupt(self, generation_id: int): ...
    async def update_tools_or_context(self, state): ...
    async def close(self): ...
```

Candidates:

- [Moshi](https://github.com/kyutai-labs/moshi): open full-duplex spoken-dialogue
  framework with parallel user/assistant streams, a streaming Mimi codec, and
  PyTorch, Rust, and MLX implementations. Its repository reports 160 ms
  theoretical and about 200 ms practical latency on an L4 GPU; this must be
  remeasured on the actual telephone and Mac path.
- [NVIDIA PersonaPlex](https://github.com/NVIDIA/personaplex): a Moshi-derived
  7B full-duplex speech-to-speech model with text role prompting and audio voice
  conditioning. Code and model licenses must both be reviewed.
- [Ultravox](https://github.com/fixie-ai/ultravox): direct speech-understanding
  model that removes a separate ASR stage but currently emits streaming text in
  the open model, so it still needs TTS.
- [speech-swift](https://github.com/soniqo/speech-swift): community Apple
  Silicon/MLX toolkit that can accelerate local experiments, including
  PersonaPlex. Treat it as newer integration code requiring independent tests.

Research watch list, not production dependencies:

- [Lychee-FD](https://github.com/HITsz-TMG/Lychee-FD);
- [SoulX-Duplug](https://github.com/Soul-AILab/SoulX-Duplug);
- [Raon-Speech](https://github.com/krafton-ai/Raon-Speech);
- [BayLing-Duplex](https://github.com/BayLing-Models/BayLing-Duplex).

Native speech models may offer better overlap, prosody, backchannels, emotion,
and response timing. They do **not** automatically provide the strongest
reasoning, multilingual coverage, tool reliability, factual grounding, or
auditability. Promote one only after it beats the cascade on the complete
acceptance scorecard, not latency alone.

The current Mac is an Apple M4 Max with 36 GB unified memory. It is suitable for
meaningful quantized 7B/MLX experiments, but real-time factor, thermal behavior,
memory pressure, voice quality, and concurrent-call capacity must be measured.

### 15.10 High-understanding conversation layer

“Any conversation” cannot literally guarantee knowledge or success for every
possible caller and environment. The production interpretation is broad,
open-ended conversation with explicit uncertainty, repair, and escalation.

Implement these capabilities above the model:

#### Turn and intent understanding

- combine acoustic activity, words, syntax, semantics, and conversation state;
- distinguish pause, completed thought, interruption, backchannel, correction,
  side speech, voicemail, and noise;
- preserve timestamps and confidence for partial/final hypotheses;
- support caller self-correction without retaining the superseded fact;
- avoid answering an incomplete sentence merely because silence exceeded one
  fixed threshold.

#### Language and entity handling

- detect and retain the active language without oscillating on every phrase;
- support required in-sentence code-switching;
- benchmark accents using real telephone audio;
- use narrow vocabulary hints for people, products, cities, and domain terms;
- verify critical names, numbers, dates, addresses, prices, and identifiers;
- read back or confirm irreversible/high-impact values before acting.

#### Memory hierarchy

```text
Audio/turn working state          expires within the live turn
Call conversation context        isolated to one call
Approved caller/customer context loaded for the call
Durable business records         written only through authorized tools
Knowledge retrieval              sourced, bounded, and freshness-aware
```

Never confuse generated-but-unheard text with conversation history. After an
interruption, truncate assistant context to the spoken prefix.

#### Uncertainty and repair

- track recognition, semantic, retrieval, and tool confidence separately;
- ask a short targeted clarification instead of inventing missing data;
- explain when a tool or provider is unavailable without claiming success;
- transfer or end safely after repeated failed repair attempts;
- log why the coordinator clarified, refused, retried, or escalated.

### 15.11 Tools and deep reasoning

Every tool must have:

- a typed schema and narrow authorization scope;
- call/session identity and an idempotency key;
- validation of caller-provided arguments;
- a deadline, cancellation behavior, and explicit result state;
- a distinction between read-only and mutating operations;
- confirmation policy for irreversible or high-impact actions;
- an audit event and redacted error handling;
- optional compensation/reversal where the business operation supports it.

The LLM may propose an operation. Only the tool execution layer can report that
it succeeded. The coordinator verbalizes success only after receiving the
confirmed result.

### 15.12 Audio and voice quality policy

Hyper-low latency is not useful if the remote caller cannot understand the
voice. Apply these rules:

- retain 20 ms gateway frames for prompt interruption and bounded queues;
- use the telephone route's true effective bandwidth as the quality ceiling;
- resample once at each unavoidable model/gateway boundary;
- avoid repeated 8/16/24/48 kHz conversion chains;
- normalize gain once and leave headroom for the carrier codec;
- monitor clipping, silence, gaps, underruns, drift, and discontinuities;
- keep TTS cadence natural while avoiding long generated pauses;
- support pronunciation dictionaries/SSML/provider equivalents where safe;
- maintain a stable voice/persona across reconnects and model fallbacks;
- benchmark voices after cellular transmission, not from local studio output;
- do not stack several noise suppression/AGC filters without A/B evidence.

Caller-only downlink capture already prevents the injected assistant voice from
feeding the STT path on the verified GSM route. Re-measure isolation for every
new route, ROM, carrier, or phone.

### 15.13 Latency budget and optimization order

The following are engineering budgets for ordinary turns, not independent
numbers to add blindly; streaming stages overlap.

| Segment | Initial budget | Optimization |
|---|---:|---|
| Phone capture to validated Mac frame | 20-60 ms | Persistent socket, 20 ms framing, no blocking work |
| Caller onset to interruption decision | 20-80 ms | Local VAD/reflex path |
| Caller onset to Android flush ack | P95 <= 150 ms | Urgent independent control channel |
| Semantic end-of-turn after true speech end | 120-350 ms | Adaptive turn model, language-specific tests |
| LLM first useful token after committed context | 80-300 ms | Warm connection, prompt/cache discipline, routing |
| TTS first usable PCM | 80-250 ms | Warm streaming session, phrase-level input |
| Mac PCM to remote audible output | 40-120 ms | Bounded queue, paced frames, carrier-dependent |
| End-of-turn to first audible response | P50 <= 500 ms; P95 <= 900 ms | Overlap safe work and eliminate cold starts |

For D1, the phone-capture and Mac-to-phone rows are measurement-only. The
working Android gateway is not an optimization target. Current evidence places
active work in Mac-side endpoint/STT timing, TTS startup, and safe text/audio
release. Warm Ollama first text is already within its component budget.

Optimization priority:

1. eliminate cold provider/model connections;
2. prevent queue growth and repeated resampling;
3. improve endpointing without cutting callers off;
4. stream LLM and TTS incrementally;
5. use safe speculative generation and measure discard cost;
6. route simple turns to a fast model and complex turns to deeper reasoning;
7. consider native speech-to-speech only after quality/tool tests.

Do not reduce endpoint delay so aggressively that understanding accuracy falls.
A fast wrong answer is a failure.

### 15.14 Evaluation and model promotion

Use three layers of evaluation:

#### Layer 1 — deterministic offline tests

- protocol encode/decode, fragmentation, corruption, replay, and wrong-call;
- cancellation races and late provider callbacks;
- generation rejection and spoken-prefix context repair;
- bounded queues, resampling, pacing, and clock drift;
- coordinator states, tool idempotency, and failure policy.

#### Layer 2 — reproducible voice benchmarks

Use [Full-Duplex-Bench](https://github.com/DanielLin94144/Full-Duplex-Bench)
for interruption, backchannel, overlap, side speech, disfluency, multi-turn, and
tool-use scenarios. Version the dataset, evaluator, model configuration, and
results so runs are comparable.

#### Layer 3 — controlled cellular corpus

Create consented GSM/VoLTE recordings containing:

- quiet and noisy environments;
- required accents, languages, and code-switching;
- short, long, emotional, hesitant, fast, and low-volume turns;
- names, numbers, dates, addresses, spelling, and corrections;
- interruption at the beginning, middle, and end of agent speech;
- backchannels that should and should not stop the agent;
- simple answers, deep reasoning, retrieval, and multi-step tools;
- disconnect, reconnect, provider failure, and process restart.

Score every candidate on:

```text
understanding:  transcript/entity accuracy, correction and intent success
conversation:   task success, factuality, context consistency, clarification
turn-taking:    false endpoints, false interruptions, backchannel behavior
latency:        remote P50/P90/P95/P99 and caller-onset-to-mute
voice:          intelligibility, pronunciation, naturalness, clipping/gaps
tools:          argument accuracy, confirmation, idempotency, verified result
operations:     CPU, memory, thermals, reconnect, errors, cost per call/minute
```

A model/backend is promoted only when the versioned scorecard improves without
breaking safety, tool accuracy, or latency gates. Retain the previous known-good
configuration for rollback.

### 15.15 Implementation work packages

#### Work package A — protocol substrate

Status on 2026-08-25: **exit condition achieved on the tested phone/ROM.** Live
control/media/generation, Android-process recovery, Mac ADB-daemon recovery,
persisted idempotency, monotonic resynchronization, authoritative system-APK
deployment, and unattended reboot survival are verified.

Deliver versioned control/media codecs, authentication, link epochs,
idempotency, sequence tracking, acknowledgements, reconnect/resync, bounded
queues, and tests. Exit when raw PCM feasibility sockets are no longer used by
the production path, failure-injection proves live reconnect/resynchronization,
and the current protocol build survives reboot from the persistent GSI.

#### Work package B — real Pipecat adapter

Status on 2026-08-25: **exit condition achieved on the tested GSM route.** The
transport processors, per-call host, offline `PipelineWorker` loopback, and real
Pipecat cellular worker traversal are implemented and verified. The live proof
captured caller audio through Pipecat, injected paced Pipecat output, performed
an authenticated generation-3 interruption flush in 38.46 ms, rejected a late
cancelled-generation frame, and cleaned up the worker and call.

Deliver Pipecat input/output transports, control adapter, call lifecycle,
interruption-to-Android flush, pacing, resampling, generation rejection,
metrics, and an offline loopback harness. Exit when a synthetic Pipecat audio
source can traverse Mac -> Android -> test sink and interruption leaves no old
audio.

#### Work package C — baseline intelligent call

Status on 2026-08-25: **functional baseline achieved; exit condition remains
open for repeated quality calls.** Provider-neutral composition, native
no-thinking Ollama streaming, context, interruption cancellation, and per-call
workers are implemented. A real MLX-Whisper/Ollama/Kokoro call completed four
assistant turns, and a real MLX-Whisper/Ollama/Edge call completed one measured
turn using the former `qwen3.8:latest`. Ollama and MLX Whisper now prewarm before
gateway readiness and provider objects are prepared before call attachment.
The production Ollama default is now `qwen3.5:4b-mlx`; its native steady-state
LLM measurements pass the component latency target, but a real cellular call
has not yet remeasured the complete turn. Kokoro failed the listener quality
test; Edge phrase streaming is the active A/B candidate. The historical
5.801-second Kokoro-call median and 5.723-second Edge turn remain above the
latency gate, and several new-model Edge calls with subjective quality verdicts
are still required.

Deliver one prewarmed streaming STT, LLM, and TTS configuration; per-call
context; coordinator; transcript; safe inbound/outbound policy; and real call
runner. Exit when several natural multi-turn calls complete with correct
context and measured timelines.

#### Work package D — bounded Mac-only latency sprint

Status on 2026-08-25: **D1 measurement foundation implemented; phone frozen.**

D1 is deliberately limited to:

- content-free timestamps and revisions from the existing streaming Flux path;
- direct use of Flux's authoritative external turn boundaries;
- preservation of uncertain completed transcripts;
- deterministic eager/resume/final state tests;
- recorded-audio evaluation and three real-call validations when the existing
  provider credentials are available.

D1 excludes Android/APK changes, a new orchestration framework, custom ASR
research, new provider adapters, advanced backchannels, native duplex models,
business tools, and dashboard UI. Speculative Ollama/TTS output remains a
conditional second slice, not part of the measurement foundation.

Exit D1 when the fixed replay set and controlled calls identify one measured
next bottleneck. Implement a speculative Mac commit gate only if traces show a
material reusable eager lead; otherwise optimize the measured STT or TTS stage.
Keep a change only when it improves latency or correctness without increasing
false endpoints or leaking cancelled audio.

#### Work package E — high understanding and tools

Deliver language/entity policies, memory, retrieval, clarification, deep-model
routing, tool schemas, confirmations, idempotency, and escalation. Exit when
Gate F and selected business scenarios pass.

#### Work package F — native duplex experiments

Deliver the common backend adapter and benchmark Moshi/PersonaPlex or the best
current candidate on the M4 Max. Compare identical cellular turns against the
cascade. Keep it experimental until it meets the entire promotion scorecard.

#### Work package G — unattended production pilot

Deliver supervision, restart recovery, provider fallback, cached critical
phrases, retention controls, security review, failure injection, 30-minute
duplex tests, and the 100-call soak. Exit only when Gate E passes.

### 15.16 Selectable LLM backends and local measurements

The Pipecat topology must not depend on one model vendor. Select the text LLM
with `PHONE_AGENT_LLM_PROVIDER`; supported values are `ollama`, `openrouter`,
`openai`, `gemini`, `gemini_cli`, and `codex_app`. STT, context aggregation,
TTS, phone media, and interruption behavior remain identical when this setting
changes.

The default is Ollama because it is local, private, does not require an API
key, and remains available during an Internet/provider outage. Local inspection
on 2026-08-25 found Ollama 0.32.15 with `qwen3.5:4b-mlx` installed at 4.0 GB and
GPU-resident with an 8,192-token context. The native production adapter prewarms
the exact runner configuration, holds it with `keep_alive=-1`, disables thinking
explicitly, and streams content incrementally. Its measured warm native median
was 72.92 ms to first text and 357.96 ms to complete a constrained short reply.
The former default, `qwen3.8:latest`, is a 27.3B Q4_K_M model occupying about
17 GB; it remains installed for rollback but its runner is unloaded. Model
promotion still requires the fixed quality and tool-use corpus plus real-call
listener evaluation rather than speed measurements alone.

`codex_app` is implemented through the supported app-server binary bundled in
`/Applications/ChatGPT.app/Contents/Resources/codex`. It does not copy tokens,
read credential files, or imitate private HTTPS traffic. The child app-server
retains responsibility for its existing ChatGPT login and exposes its generated
line-delimited JSON-RPC interface over stdio. Local inspection found:

- desktop app command: bundled `codex app-server --analytics-default-enabled`;
- CLI and bundle family: Codex 0.149.0;
- authentication status: ChatGPT account, Pro plan, no project API key;
- available models: GPT-5.6 Sol, Terra, Luna, GPT-5.5, GPT-5.4, GPT-5.4 Mini,
  and GPT-5.3 Codex Spark;
- required methods: `initialize`, `account/read`, `model/list`, `thread/start`,
  `turn/start`, `turn/interrupt`, `item/agentMessage/delta`, and
  `turn/completed`.

The adapter starts an ephemeral thread in `/tmp`, uses a read-only sandbox,
disables dynamic tools and environments, rejects unexpected server tool
requests, and streams only assistant message deltas into Pipecat. A real local
probe using GPT-5.6 Luna returned `Adapter ready.` with about 4.45 seconds to
first text and 4.55 seconds total. Other probes ranged from 3.8 to 5.2 seconds,
and one second turn timed out. This backend is therefore a high-intelligence
experimental fallback, not the hyper-low-latency live-call default. It may be
promoted only if future persistent-session measurements pass the same P95
latency and reliability gates as other providers.

#### Local Google applications: Antigravity versus Gemini CLI

Local inspection on 2026-08-25 found Antigravity 2.9.1 at
`/Applications/Antigravity.app`. Its signed Electron wrapper starts the bundled
`Contents/Resources/bin/language_server` in standalone mode. The server owns
Google login, fetches the account's available model list, serves the product UI
over ephemeral loopback HTTPS, and uses generated CSRF and host-bridge bearer
tokens. The wrapper also has a headless mode, but testing showed that it only
forwards stdin to the language server; it does not expose model response deltas
on stdout. Unlike Codex, the bundle does not advertise a public app-server,
schema generator, stable JSON-RPC interface, or OpenAI-compatible endpoint.

Therefore **do not implement an `antigravity_app` provider** by scraping tokens,
reading OAuth files, copying cookies, disabling TLS, or cloning private
Cloud Code/ConnectRPC calls. Those approaches are insecure, likely to break on
updates, may violate service terms, and cannot satisfy the production latency
or reliability gates. A future Antigravity provider is allowed only if Google
ships a documented local API/CLI that supports streaming, cancellation,
conversation isolation, tool disablement, and account-authorized model listing.

The safe no-API-key Google option is `gemini_cli`. It invokes the separately
installed official `gemini` command and leaves its supported Google OAuth flow
responsible for authentication. Each request:

- runs in an isolated temporary workspace;
- disables core tools, MCP servers, context files, telemetry, and usage stats;
- passes the full conversation through stdin so caller content is absent from
  process arguments;
- streams stdout into Pipecat and terminates the child on timeout or barge-in;
- redacts common credential and email shapes from surfaced CLI errors.

Select it with:

```bash
export PHONE_AGENT_LLM_PROVIDER=gemini_cli
export PHONE_AGENT_LLM_MODEL=gemini-2.5-flash
```

This is not an Antigravity entitlement bridge. The installed Gemini CLI 0.1.9
is configured for Google OAuth, but its local smoke test currently stops before
model selection because this account requires `GOOGLE_CLOUD_PROJECT`. No model
latency claim is valid until that supported account configuration is completed.
The requested `gemini-3.7-flash` name was not exposed by the installed CLI or
static Antigravity bundle, so it must not be declared available based only on a
UI label; verify the provider's exact model ID through a supported model-listing
interface before configuring production.

### 15.17 Primary technical references

- [Pipecat repository](https://github.com/pipecat-ai/pipecat)
- [Pipecat pipelines and frames](https://docs.pipecat.ai/pipecat/learn/pipeline)
- [Pipecat transports](https://docs.pipecat.ai/pipecat/learn/transports)
- [Pipecat WebSocket transport and custom serialization](https://docs.pipecat.ai/api-reference/server/services/transport/websocket-server)
- [Pipecat speech input and Smart Turn](https://docs.pipecat.ai/pipecat/learn/speech-input)
- [Pipecat interruption lifecycle](https://docs.pipecat.ai/pipecat/fundamentals/interruptions)
- [LiveKit Agents sessions](https://docs.livekit.io/agents/logic/sessions/)
- [LiveKit turn detection and interruptions](https://docs.livekit.io/agents/logic/turns/)
- [LiveKit turn-taking tuning](https://docs.livekit.io/agents/logic/turns/tuning/)
- [Moshi](https://github.com/kyutai-labs/moshi)
- [PersonaPlex](https://github.com/NVIDIA/personaplex)
- [Ultravox](https://github.com/fixie-ai/ultravox)
- [Full-Duplex-Bench](https://github.com/DanielLin94144/Full-Duplex-Bench)
