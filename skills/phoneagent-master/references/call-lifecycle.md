# Call, Audio and Conversation Lifecycle

## Channel meanings

| Channel value | Call placement | Audio path | Android/GSM dependency |
| --- | --- | --- | --- |
| `gsm` | Android Telecom | privileged telephony bridge | Required |
| `whatsapp_phone` | WhatsApp Android UI via ADB | Android VoIP route into existing bridge | Android required; no GSM dialing |
| `whatsapp` | native Rust `whatsapp-rust` sidecar | direct two-way 16 kHz PCM | Bypasses Android and GSM |

OpenWA is not a voice channel. It is a WhatsApp messaging companion used during any authenticated
current-caller conversation.

## Persistent Studio lifecycle

1. macOS `launchd` starts `phone-agent-web` from the self-contained installed runtime.
2. Studio loads saved runtime, task, identity and integration configs.
3. If auto-answer is enabled and no outbound call owns the lock, Studio starts an inbound
   `phone-agent-voice` child.
4. The child prewarms cascade models or preloads the selected Realtime transport before declaring
   inbound readiness.
5. Studio receives structured `PHONE_AGENT_EVENT` lines from the child and broadcasts them to the UI
   and bounded external-agent event cursor.

## Outbound GSM lifecycle

1. Studio or the admin MCP validates/normalizes the destination through `CallPolicy`.
2. The one-call lock and hardware preflight must pass.
3. Studio pauses the inbound receptionist and starts a one-call child with the selected AgentPackage
   configuration in its environment.
4. For Realtime GSM, the child begins the OpenAI handshake before asking Android to dial, overlapping
   model setup with carrier ringing.
5. Android reports DIALING → CONNECTING → ACTIVE.
6. The child connects media, verifies both remote capture and telephony injection routes, and refuses
   the call if the caller would hear silence.
7. The opening response is generated once. Playback completion—not generated text—determines what the
   caller actually heard.
8. On remote hang-up or AI `end_call`, the child closes media and exits; Studio returns to IDLE and
   restarts inbound listening.

## Inbound GSM lifecycle

1. The persistent child polls authenticated Android call state.
2. RINGING causes Realtime preconnect before answer.
3. When auto-answer is enabled, PhoneAgent gives preconnect a bounded head start and answers through
   the Android control channel.
4. The incoming number becomes authenticated current-caller metadata.
5. ACTIVE attaches media and starts the intent-led call context.
6. When the call ends, the same child replaces its runtime and returns to listening without requiring
   a Studio restart.

If Studio says listening while `adb devices` is empty, do not trust the label alone. Re-run device
qualification and verify the gateway.

## Direct WhatsApp voice lifecycle

The Rust sidecar owns WhatsApp signaling, persistent linked-device state and PCM. The host waits for
peer acceptance before greeting. Its implementation and session protocol are frozen. Do not route
direct WhatsApp through Android, OpenWA or GSM as a repair shortcut.

## Authenticated media protocol

- Every call has a UUID, link epoch and generation.
- Control commands are authenticated and replay-resistant.
- Media frames identify kind, direction, sequence, generation and payload.
- Generation advances invalidate stale queued audio after interruption/recovery.
- Android acknowledges frames when its playout writer consumes them.
- A bounded credit window prevents unlimited buffering.
- Explicit end-of-speech markers avoid using queue emptiness as sentence structure.

The authoritative playout clock is Android, not model generation time, Mac socket write time or UI
transcript time.

## Realtime WebSocket path

- Phone 16 kHz PCM is resampled statefully to 24 kHz for OpenAI.
- OpenAI 24 kHz audio is resampled to phone-ready 16 kHz PCM.
- A small startup verifier observes initial human energy without changing audio.
- Realtime server VAD normally owns turn boundaries; semantic VAD is available as a validated option.
- The model hears audio directly. Input transcription is side-channel evidence for UI, task state,
  memory and exact-text grounding; it must not independently create a second caller turn.
- Tool-only responses explicitly request a follow-up response because they produce no audio.
- Reconnect replays a bounded conversation log and preserves generation/cancellation boundaries.

## WebRTC compatibility path

- Phone PCM is resampled to 48 kHz RTP audio.
- Realtime control uses a data channel while RTP carries media.
- RTP and control completion can arrive independently; a fixed settle boundary handles in-flight
  packets.
- It uses the same identity, task, caller binding, tools, grounding and AI hang-up semantics as the
  WebSocket path.

When changing shared Realtime behavior, test both transports.

## Cascade path

`ProductionCallPipeline` assembles selected STT, LLM and TTS services through Pipecat. It supports
local and remote providers, model prewarming, transcription policy, repair/reflex processors,
streamed speech, playback reporting and the same phone transport. Voice/Models Studio controls mostly
apply here; the Realtime voice is selected in Pipeline & S2S.

## Turn ownership and interruption

1. Caller speech starts an acoustic epoch.
2. Assistant output is cancelled and the phone generation advances.
3. Stale model/RTP/PCM output from the previous generation is dropped.
4. Only one authoritative finalized caller turn enters task state and memory.
5. Late transcription corrections without new acoustic speech replace/suppress earlier hypotheses;
   they do not create duplicate turns.
6. Playback reports generated, playing, completed or interrupted with an approximate heard duration.

Never repair duplicate/false speech by increasing latency blindly. Determine whether the source was
echo, startup audio, a duplicate transcription, stale generation or genuine caller barge-in.

## Tool lifecycle during a call

1. The task allowlist and active integration configs build the offered tool catalog.
2. The Realtime model decides whether to call a tool.
3. PhoneAgent grounds explicitly dictated durable text against recent authoritative caller turns.
4. Low-confidence mismatches are blocked before the write.
5. The bounded handler executes and returns structured evidence.
6. The model receives the result and may describe only the state it verifies.
7. Tool events carry call identity, direction and channel for diagnostics.

Generic tools, OpenWA, research and Frappe runtimes hot-attach without blocking audio startup.

## AI-controlled ending

The model owns the conversational decision that the interaction is complete. It calls `end_call`
with a reason and closing message. PhoneAgent creates one terminal response, waits for its phone
playout boundary, then requests call completion exactly once. Regexes may detect context such as a
goodbye for state/evaluation, but they do not hang up instead of the AI.

Verify a successful ending through this event order:

1. `tool_call` for `end_call` returns `accepted: true`.
2. `ai_end_call_requested` appears.
3. Terminal assistant response is generated and played.
4. `call_completion` appears.
5. Android/sidecar transitions to disconnected/idle.
6. Studio returns to IDLE and inbound listening.

## Key audio diagnostics

- input/output frames and bytes
- dropped/stale frames and sequence gaps
- generation and flush latency
- capture source/proof and injection route/proof
- queue depth, credits, rendered sequence and underruns
- caller RMS/peak/speech/silence/clipping
- echo correlation and suppressed frames
- interruption count and caller-heard duration

Transcript fidelity is not audio-path proof. A real remote endpoint is needed to prove that the
caller heard injected audio.
