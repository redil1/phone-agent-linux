# PhoneAgent Master Persona, Task & Realtime S2S Engineering Guide

This document is the production reference for creating, customizing, and running telephone
agents in PhoneAgent. It covers the OpenAI Realtime server-to-server WebSocket engine and the
modular cascade engine. The OpenAI API documentation remains authoritative for provider event
schemas.

---

## 1. Complete System Architecture

PhoneAgent decouples **Identity (Persona)** from **Objective (Task Contract)** and binds them deterministically into the voice stream:

```
┌────────────────────────────────────────────────────────────────────────┐
│ 1. Persona Definition: persona.yaml                                    │
│    Identity (Aziz), Voice Tone, Negotiation Style, Constraints         │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
┌──────────────────────────────────▼─────────────────────────────────────┐
│ 2. Task Contract: tasks/contracts/<task_id>.yaml                       │
│    Objective, Opening Hook, Pricing Table, Slot Fields, Objection Rules│
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 3. PersonaCompiler (ai_bridge/personality/persona_compiler.py)         │
│    Compiles structured YAML into a dense, executable System Prompt     │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                  ┌────────────────┴────────────────┐
                  ▼                                 ▼
┌──────────────────────────────────┐  ┌──────────────────────────────────┐
│ Mode A: OpenAI Realtime S2S      │  │ Mode B: Modular Cascade          │
│ • WebSocket PCM 24 kHz           │  │ • Independent STT / LLM / TTS    │
│ • Clean per-call context         │  │ • Local or hosted providers      │
│ • Deterministic server VAD       │  │ • Explicit text policy pipeline  │
│ • Android-clock interruption     │  │ • Provider-specific endpointing  │
└──────────────────────────────────┘  └──────────────────────────────────┘
```

---

## 2. OpenAI Developer Realtime S2S Engine Specification

The preferred S2S engine connects the phone's 16 kHz cellular PCM stream directly to OpenAI's
Realtime API over a server-to-server WebSocket. `PHONE_AGENT_CHATGPT_TRANSPORT=webrtc` keeps the
older WebRTC implementation available as a compatibility rollback.

### 2.1 Connection and credentials

* WebSocket URL: `wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1`
* Preferred authentication: `Authorization: Bearer $OPENAI_API_KEY`
* Compatibility authentication: the existing local OAuth manager when no API key is configured
* Stable abuse-monitoring identifier: a one-way hash of the call's caller identifier
* Default model: `auto`, currently resolved by the application to `gpt-realtime-2.1`

### 2.2 PCM and control architecture

1. Caller audio remains un-gated and is statefully resampled from 16 kHz PCM to 24 kHz PCM.
2. Two 20 ms phone frames are batched per `input_audio_buffer.append` event.
3. Assistant bytes are consumed only from `response.output_audio.delta`, statefully resampled
   to 16 kHz, and split into exact 640-byte Android frames.
4. `response.output_audio.done` flushes the resampler tail before an ordered phone end marker.
5. Android acknowledges the marker only after `AudioTrack` renders all preceding audio.

---

### 2.3 Session-Level Persona Binding (`session.update`)

Immediately after the WebSocket opens, PhoneAgent binds a compact Realtime-specific persona and
task prompt. It intentionally excludes local caller memory and any account history.

```json
{
  "type": "session.update",
  "session": {
    "type": "realtime",
    "model": "gpt-realtime-2.1",
    "instructions": "<COMPACT PERSONA, TASK, FACTS, AND LIVE CALL STATE>",
    "output_modalities": ["audio"],
    "reasoning": {"effort": "low"},
    "audio": {
      "input": {
        "format": {"type": "audio/pcm", "rate": 24000},
        "transcription": {"model": "gpt-live-transcribe", "delay": "low"},
        "turn_detection": {
          "type": "server_vad",
          "threshold": 0.5,
          "prefix_padding_ms": 300,
          "silence_duration_ms": 450,
          "create_response": true,
          "interrupt_response": true
        }
      },
      "output": {
        "format": {"type": "audio/pcm", "rate": 24000},
        "voice": "alloy"
      }
    }
  }
}
```

The call does not start until `session.updated` confirms the session configuration. Transcription
is used for Studio visibility and code-owned task state only; it never creates or delays the
native response.

---

### 2.4 Outbound Sales Opening Trigger (`response.create`)

When the customer answers the phone (e.g. says *"Allô ?"*), PhoneAgent programmatically triggers the opening sales pitch:

```json
{
  "type": "response.create",
  "response": {
    "instructions": "The customer just answered the phone. Introduce yourself clearly and speak your opening greeting: 'Bonjour, c'est Aziz de chez OXzoon. Je vous appelle au sujet de nos abonnements IPTV. Est-ce un bon moment pour échanger ?'"
  }
}
```

---

### 2.5 Wire Protocol Event Handling Table

| OpenAI Realtime GA Event | Description | Action in PhoneAgent Gateway |
| :--- | :--- | :--- |
| `session.updated` | Persona and media configuration accepted | Releases startup readiness. |
| `response.output_audio_transcript.delta` | Live assistant text token | Streams delta to Studio Web UI & logs turn text. |
| `response.output_audio.delta` | Assistant PCM bytes | Resamples and queues exact phone frames. |
| `input_audio_buffer.speech_started` | Semantic VAD detected caller speech | Immediately drops local output, flushes Android, and sends one exact truncation. |
| `response.output_audio.done` | No more response audio deltas | Flushes resampler tail and queues the ordered render marker. |
| `response.output_audio_transcript.done` | Assistant transcript complete | Records the exact text already spoken; no post-audio rewrite. |
| `conversation.item.input_audio_transcription.completed` | Side transcription complete | Updates task/UI asynchronously without calling `response.create`. |

---

## 3. How to Create or Customize a Persona (`persona.yaml`)

The persona file defines identity, communication tone, and behavioral boundaries.

### File Locations
* **Default Base**: `ai_bridge/personality/persona.yaml`
* **User Override**: `~/.config/phone-agent/persona.yaml`

### Complete Production Example

```yaml
version: "1.0"

# 1. Identity & Corporate Role
identity:
  name: "Aziz"
  role: "Senior Sales Director"
  organization: "OXzoon"
  industry: "Premium IPTV & Digital Streaming"
  location: "Paris, France"
  background: >
    Experienced digital media and telecoms professional with 8+ years specializing
    in premium IPTV subscriptions, sports broadcasting rights, and customer streaming setup.

# 2. Personality Trait Intensities (0.0 to 1.0)
trait_intensity:
  analytical: 0.90
  empathetic: 0.85
  direct: 0.88
  professional: 0.95
  humorous: 0.20

# 3. Decision Precedence
decision_priority:
  - "factual_correctness"       # Never invent false specs or pricing
  - "achievement_of_objective"  # Keep the caller focused on the task goal
  - "caller_comfort_and_trust"  # Be respectful, polite, and listen actively
  - "conciseness"               # Keep spoken turns short (1-2 sentences)

# 4. Spoken Communication Rules
communication_style:
  pace: "moderate"
  max_words_per_turn: 35
  language: "fr-FR"
  secondary_languages: ["en-US"]
  tone: "warm, authoritative, and consultative"
  disallowed_phrases:
    - "Comment puis-je vous aider en tant qu'intelligence artificielle"
    - "As an AI language model"
    - "What is on your mind today"
    - "En quoi puis-je être utile aujourd'hui"
  mandated_habits:
    - "Always speak in active voice with confidence."
    - "Acknowledge the caller's answers warmly before asking the next qualification question."
    - "Quote exact pricing strictly from the Knowledge Base."
```

---

## 4. How to Create or Customize a Task Contract

Tasks define the business workflow, qualification questions (slots), pricing, and objection strategies.

### File Location
* `ai_bridge/tasks/contracts/<task_id>.yaml`

### Complete Production Example: `iptv_subscription_sales.yaml`

```yaml
id: "iptv_subscription_sales"
title: "IPTV Subscription Outbound Prospecting & Sales Qualification"
version: "1.0.0"

objective: >
  Conduct an outbound sales prospecting call to qualify the customer's home entertainment
  setup (TV, devices, number of screens) and sell an OXzoon Premium IPTV subscription.

# Opening greetings for outbound calls
opening_greeting:
  fr: "Bonjour ! C'est Aziz de chez OXzoon. Je vous appelle car nous venons de lancer notre nouvelle offre IPTV 4K sport et cinéma sans coupure. Est-ce que vous regardez les matchs de foot ou des films en streaming chez vous ?"
  en: "Hello! This is Aziz from OXzoon. I'm reaching out because we just launched our new 4K IPTV service for live sports and cinema without buffering. Do you watch live sports or movies at home?"

# Ground-Truth Knowledge Base (AI must strictly adhere to these prices)
knowledge:
  provider: "OXzoon IPTV"
  infrastructure: "High-bandwidth anti-freeze server cluster with 99.9% uptime"
  channel_count: "Over 18,000 live channels in 4K and Full HD"
  vod_count: "Over 60,000 movies and series updated daily with French & multi-audio"
  supported_devices: "Smart TV (Samsung, LG), Android TV, Fire TV Stick, Apple TV, MAG, Smartphone, PC"
  plan_essential: "10€ / month (1 screen, 4K Sports & Cinema, 24/7 EPG)"
  plan_family: "15€ / month (3 simultaneous screens, 4K & UHD, multi-room)"
  plan_premium_annual: "80€ / year (3 screens, full access, VIP priority support)"
  free_trial: "24-hour instant test account available upon request"

# Slots to Qualify
inputs_required:
  - id: "device_type"
    type: "string"
    question: "Quel appareil utilisez-vous principalement pour regarder la télévision (Smart TV, Fire Stick, Box Android ou smartphone) ?"
    required: true

  - id: "screen_count"
    type: "integer"
    question: "Combien d'écrans ou de téléviseurs souhaitez-vous connecter en simultané dans votre foyer ?"
    required: true

  - id: "content_preference"
    type: "string"
    question: "Qu'est-ce que vous regardez le plus souvent : les chaînes de sport (Ligue 1, Champions League) ou les films et séries ?"
    required: false

# Conversation Stages
stages:
  - id: "opening_hook"
    description: "Greet the customer, introduce Aziz and OXzoon, and establish rapport with the hook question."
    exit_condition: "Customer responds to the opening hook."

  - id: "qualification"
    description: "Discover devices, screen count, and content preferences."
    exit_condition: "device_type and screen_count slots are populated."

  - id: "pricing_pitch"
    description: "Recommend Essential 10€, Family 15€, or Premium 20€ when it fits."
    exit_condition: "Customer acknowledges pricing."

  - id: "closing"
    description: "Secure agreement for a 48-hour trial or a verified next step."
    exit_condition: "Customer accepts trial or confirms plan."

# Objection Handling Matrix
objection_handling:
  - objection: "J'ai peur que ça coupe pendant les gros matchs de foot."
    rebuttal: "C'est une préoccupation importante. Je ne vais pas vous promettre zéro coupure, mais le test de 48 heures vous permet de vérifier le service avant de payer."

  - objection: "C'est trop cher."
    rebuttal: "Le forfait Essential est à 10 euros par mois pour un écran. Pour que je vous conseille honnêtement, quel budget aviez-vous en tête ?"

  - objection: "Je ne sais pas comment l'installer."
    rebuttal: "L'activation prend environ dix minutes une fois les informations confirmées, et le service fonctionne sur Smart TV, Firestick, Apple TV, box Android, téléphone et tablette."
```

---

## 5. End-to-End implementation reference

The maintained implementation is `ai_bridge/openai_realtime_websocket_pipeline.py`. Keep
protocol behavior in that module and its regression tests instead of duplicating a second
implementation in documentation. Its public lifecycle is `start()`, `greet()`,
`send_text_message()`, `stop()`, and `cancel()`.

Production defaults:

```dotenv
PHONE_AGENT_PIPELINE_MODE=s2s_chatgpt_realtime
PHONE_AGENT_CHATGPT_TRANSPORT=websocket
PHONE_AGENT_CHATGPT_MODEL=auto
PHONE_AGENT_CHATGPT_REASONING_EFFORT=low
PHONE_AGENT_CHATGPT_VOICE=alloy
PHONE_AGENT_CHATGPT_TRANSCRIPTION_MODEL=gpt-live-transcribe
PHONE_AGENT_CHATGPT_INPUT_LANGUAGES=en,fr
PHONE_AGENT_CHATGPT_NOISE_REDUCTION=off
PHONE_AGENT_CHATGPT_VAD_MODE=server_vad
PHONE_AGENT_CHATGPT_VAD_THRESHOLD=0.5
PHONE_AGENT_CHATGPT_VAD_PREFIX_MS=300
PHONE_AGENT_CHATGPT_VAD_SILENCE_MS=450
# Used only with PHONE_AGENT_CHATGPT_VAD_MODE=semantic_vad.
PHONE_AGENT_CHATGPT_VAD_EAGERNESS=medium
```

---

## 6. How to Run and Test

1. **Start the Web Studio Server**:
   ```bash
   uv run python -m phone_agent_gateway.ai_bridge.web_server
   ```
2. **Access Web Studio**:
   * Open `http://127.0.0.1:8090` in your browser.
   * Select **OpenAI Realtime S2S (Speech-to-Speech)** as the pipeline mode.
   * Enter the destination phone number and click **Dial**.
3. **Run Full Test Suite**:
   ```bash
   uv run pytest -q tests/
   ```
