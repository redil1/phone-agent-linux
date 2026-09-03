# PhoneAgent Studio User Guide

## A beginner-friendly handbook for every WebUI option

PhoneAgent Studio is the control center for the AI telephone agent. It lets you place calls,
watch conversations, define the agent's identity, select its task, manage memory, research a
product, and configure speech and AI providers.

This guide assumes you are the operator or business owner, not a software developer. For every
important option it explains:

1. **What it means.**
2. **Why it exists.**
3. **How to use it safely.**

The Studio is available at `http://127.0.0.1:8090/` while the PhoneAgent service is running.

---

## 1. The safest way to think about PhoneAgent

PhoneAgent separates four ideas that are often confused:

- **Identity:** Who the agent is and how it behaves.
- **Task:** What the agent must accomplish during this type of call.
- **Pipeline:** How audio and intelligence are technically processed.
- **Call channel:** Which telephone route carries the call: GSM or one of the WhatsApp routes.

A useful example is:

- Identity: “Adam, a professional and honest IPTV sales consultant.”
- Task: “Discover the caller's viewing needs and recommend a verified subscription.”
- Pipeline: “Universal Cascade (STT → LLM → TTS).”
- Channel: “Direct WhatsApp Rust media.”

Changing the task should not change Adam's identity. Changing the voice should not change the
sales goal. Changing from GSM to WhatsApp should not change the approved identity.

### The beginner's golden rules

1. Use **Live Call** for normal calls.
2. Use **Identity** when you want to change who the agent is or how it behaves.
3. Use **Tasks** when you want a different call objective.
4. Do not change **Pipeline**, **Voice**, **Models**, or **Legacy Behavior** casually when calls are
   already working well.
5. Run the Contract Check before approval. Warnings advise you but do not replace your decision.
6. Never enable recording unless the legal and consent statement is true.
7. When unsure, describe the business result you want and ask the developer to configure it.

---

## 2. Global navigation and status

The left navigation divides the Studio into workspaces.

| Workspace | Purpose | Typical user |
| --- | --- | --- |
| Live Call | Place and monitor calls | Operator |
| Identity | Control persona, boundaries, skills and approved memory | Owner or developer |
| Memory | Inspect caller history and preferences | Operator or owner |
| Tasks | Define what each call should achieve | Owner or developer |
| Product Research | Build verified product knowledge from a website | Owner or researcher |
| Pipeline & S2S | Choose the technical speech architecture | Developer |
| Voice | Choose cascade TTS voices and language | Developer or advanced operator |
| Models | Choose cascade intelligence and latency helpers | Developer |
| Legacy Behavior | Maintain older conversation rules | Developer only |

### Connected status

**What it means:** The browser has a live connection to the local PhoneAgent service.

**Why it exists:** Without this connection, the WebUI cannot receive live call states, transcripts,
audio levels, approvals or identity updates.

**How to use it:**

- `Connected` is healthy.
- `Reconnecting` means the browser temporarily lost the local service.
- If it does not reconnect, restart PhoneAgent and reload the page.

### Call status

The second status badge reports the call state.

| State | Meaning |
| --- | --- |
| IDLE | No call is active |
| DIALING | PhoneAgent is placing the call |
| RINGING | The destination is being alerted |
| ACTIVE | Two-way conversation is active |
| ENDING | The call is shutting down |
| FAILED | The call could not continue |

This state comes from the runtime. It is not a decorative animation.

### Call context status

During a call, Studio automatically shows one of these modes:

- **Outbound · cold prospecting:** PhoneAgent initiated the call. Permission is not interest; the
  agent must build relevance and explicit interest before asking product-fit questions.
- **Inbound · caller intent:** The other person initiated the call. The agent confirms why they
  called and may move directly into relevant assistance or qualification.

This status is automatic. It is based on who initiated the call, not on whether the channel is GSM
or WhatsApp. While Studio is idle it shows **Outbound · cold prospecting**, because pressing the
WebUI Call button will create an outbound call. When an incoming call arrives it changes to
**Inbound · caller intent**.

### AI answers incoming GSM calls

**What it means:** Keeps a persistent, authenticated AI receptionist connected to the Android GSM
gateway while Studio is idle.

**Why it exists:** Without the persistent voice host, Studio can place outbound calls but nothing is
running to detect, answer and attach AI media to an incoming call.

**How to use it:** Enable the checkbox in **Live Call**. Wait until the status says **Listening for
incoming GSM calls**. When the phone rings, PhoneAgent answers through the Android gateway, starts
the selected AI pipeline and uses the inbound caller-intent greeting and strategy.

The receptionist always monitors GSM, even if the selected outbound channel is WhatsApp. It pauses
before an outbound call and restarts afterward. Incoming recording remains off because the WebUI
cannot pre-confirm the inbound caller's recording consent.

| Receptionist status | Meaning |
| --- | --- |
| Disabled | Auto-answer is off |
| Starting | The voice host is connecting and preparing providers |
| Listening | Ready to answer an incoming GSM call |
| Paused | Temporarily stopped for an outbound call |
| Restarting | The supervisor is recovering after an unexpected exit |
| Error | The receptionist could not start; inspect the displayed reason |

---

## Part I — Operating calls

## 3. Live Call workspace

Use this workspace for everyday operation.

### Phone number

**What it means:** The destination you want to call.

**Why it exists:** PhoneAgent needs an exact destination before it can ask the selected channel to
dial.

**How to use it:**

- Enter the full international number.
- For Morocco, a number may be entered in a normalized international form such as `+212...`.
- Check every digit before pressing **Call**.
- Do not test with a real person unless the call is expected and authorized.

### Dialpad buttons

**What they mean:** The numbers `0–9`, `*`, and `#` append digits to the destination field.

**Why they exist:** They provide a familiar telephone interface and support destinations or service
codes that contain `*` or `#`.

**How to use them:** Click the digits in order. You may also type directly into the phone-number
field.

### Ready to dial / call state label

**What it means:** A plain-language description of the current call operation.

**Why it exists:** The global status badge is short. This label can show a more useful reason when a
call cannot start.

**How to use it:** Read this line first when pressing **Call** does not work. It may report an
identity gate, device problem, unavailable channel, or policy refusal.

### Call timer

**What it means:** The elapsed duration of the current active call.

**Why it exists:** The operator needs to know how long the conversation has lasted.

**How to use it:** The timer begins when the call becomes active and returns to `00:00` after the
call ends.

### Call channel

The channel decides how the call is placed and how audio reaches the AI.

#### Phone — cellular (GSM)

**What it means:** The rooted Android phone places a normal cellular call.

**Why it exists:** This is the carrier telephone route. It does not require the recipient to use
WhatsApp.

**How to use it:** Select it only when the qualified Android GSM gateway is connected and healthy.
Do not change GSM technical settings from this workspace.

#### WhatsApp — placed by the phone (two-way)

**What it means:** Android WhatsApp places the call through its user interface. Audio uses the
existing authenticated Android media bridge.

**Why it exists:** It keeps WhatsApp dialing on the linked Android phone while providing two-way AI
audio.

**How to use it:** Select it when the Android WhatsApp route is paired, connected and known to work.

#### WhatsApp — direct Rust media (two-way)

**What it means:** The Mac's direct Rust WhatsApp process places the call and carries two-way media.

**Why it exists:** It bypasses Android dialing and GSM while retaining direct full-duplex WhatsApp
audio.

**How to use it:** Select it when the WhatsApp account is already paired and the channel status is
available.

#### Unavailable channel label

**What it means:** The route exists but is not currently usable.

**Why it exists:** Hiding a broken channel would also hide the information needed to repair it.

**How to use it:** Read the reason shown below the menu. Do not repeatedly press **Call** until the
reported pairing, binary, device or connection problem is fixed.

### WhatsApp number and Pair

These controls appear only when the direct WhatsApp channel needs pairing.

**WhatsApp number**

- What: The WhatsApp account number being linked.
- Why: WhatsApp uses it to create a temporary pairing code.
- Use: Enter the account's full international number.

**Pair**

- What: Requests a temporary WhatsApp linking code.
- Why: The direct caller needs an authenticated session.
- Use: Press once, then follow the on-screen instructions in WhatsApp under **Linked Devices**.

**Pairing code**

- What: A temporary code displayed by PhoneAgent.
- Why: It proves that you control the WhatsApp account being linked.
- Use: Enter it only in the official WhatsApp linked-device flow. Do not send it to another person.

### Record this call

**What it means:** Enables recording for this single call.

**Why it exists:** Recording must be deliberate and consent-aware. It is not enabled automatically.

**How to use it:** Check it only when the statement beside it is completely true: recording is
lawful and every required participant has consented. Leave it unchecked if you are uncertain.

### Call

**What it means:** Requests an outbound call through the selected channel.

**Why it exists:** Dialing is an explicit operator action. The backend still checks identity
readiness, policy, channel health, device readiness and approval rules.

**How to use it:** Verify the number, channel and recording choice, then press once. Wait for the
state to change instead of pressing repeatedly.

### Hang Up

**What it means:** Ends the call owned by this Studio instance.

**Why it exists:** The operator always needs a clear, immediate way to stop a call.

**How to use it:** Press it when the call should end, the caller asks to stop, or the conversation is
not behaving safely.

### Acoustic Audio Monitor

**What it means:** A live level and waveform view shown during active calls.

**Why it exists:** It helps diagnose silence, clipping, missing audio or an inactive media route.

**How to use it:**

- Movement indicates audio activity.
- A permanently flat monitor during speech suggests a capture or media problem.
- Constant maximum level may indicate clipping or noise.
- It is a diagnostic aid, not proof that the remote person heard perfect audio.

### Live Speech Dialogue

**What it means:** A chronological transcript of caller speech, AI responses and call notices.

**Why it exists:** The operator can follow what the model understood, what it answered and whether
the response was delivered.

**How to use it:** Watch for misunderstanding, repetition, unsafe claims, language errors and
delivery failures.

### Caller messages

Caller turns may include:

- detected language;
- transcription confidence;
- a note that low-confidence audio was verified against the original signal.

These details explain how confidently the system understood the caller.

### AI Agent messages

AI turns may include:

- opening-greeting status;
- response latency;
- personality fidelity;
- playback state such as preparing, playing, completed, interrupted or not delivered.

`Delivered completely` means the local media runtime finished delivery. It does not by itself prove
the remote person's subjective audio quality.

### Personality Fidelity

**What it means:** A score for how closely the response matches the configured persona.

**Why it exists:** A response can be factually useful but still sound unlike the intended agent.

**How to use it:** Look for repeated low scores. One unusual turn may be harmless; a pattern suggests
the Identity examples, voice limits or task instructions need improvement.

### Task Safety Score

**What it means:** A score for how well the response respects the active task's requirements and
safety rules.

**Why it exists:** It helps detect unsupported facts, missing permission, unauthorized commitments
or departure from the call objective.

**How to use it:** Treat a low score as a reason to review the conversation before making more calls.

### Clear

**What it means:** Clears the transcript currently displayed in the browser.

**Why it exists:** It gives the operator a clean view for the next observation.

**How to use it:** Press it only when you no longer need the visible transcript. It does not delete
recordings, backend audit records or approved memory.

---

## Part II — Defining the agent

## 4. Identity workspace

Identity controls who the agent is, its permanent principles, speaking style, trusted skills and
approved long-term context.

The active Identity cannot be silently edited. Changes become a new revision and must pass a staged
workflow before they can affect calls.

### Active Identity Constitution

#### Production ready

**What it means:** The active version passed the evaluations required by production policy.

**Why it exists:** A saved persona should not reach real calls merely because its JSON is valid.

**How to use it:** Make normal calls only when this status says production ready.

#### Version

**What it means:** The active identity revision number.

**Why it exists:** It provides a simple history: version 2 comes after version 1.

**How to use it:** Record the version when comparing behavior before and after a change.

#### Hash

**What it means:** A SHA-256 fingerprint of the complete Identity profile.

**Why it exists:** Approval must apply to the exact tested content, not a similar-looking edit.

**How to use it:** You normally do not type it. Confirm that the approved and activated hashes match.

#### Evaluation score

**What it means:** The current profile's quality score out of 100.

**Why it exists:** It summarizes identity consistency, multilingual behavior, safety, tool selection
and naturalness.

**How to use it:** Production activation requires a passing report. A score alone is not enough if a
critical check failed.

#### Memory mode

**What it means:** Whether the Identity Kernel uses local memory only or also mirrors to Graphiti.

**Why it exists:** Long-term memory is intentionally outside the audio-critical path and may have
different deployment modes.

**How to use it:** `local_only` is normal when Graphiti is not configured.

#### Episodes

**What it means:** The number of locally stored long-term conversation episodes.

**Why it exists:** It shows whether the asynchronous memory writer has recorded useful caller turns.

**How to use it:** A zero count is not an error if no qualifying calls have been processed.

#### Archived versions

**What they mean:** Older active identities preserved during activation.

**Why they exist:** You need a safe path back if a new identity performs poorly.

**How to use them:** Press **Restore & Activate v…**, then confirm the action. PhoneAgent runs the
Contract Check and activates that exact archived version with its original version number and hash;
it does not create a copy with a new number. The current identity is archived first, so nothing is
lost, and the WebUI fields refresh from the restored version. Restore is blocked during an active
call. Other unfinished drafts are rejected because they were based on the identity that was just
replaced.

### Identity workflow track

The track shows the controlled release process:

1. **Stage:** Save edits as a candidate revision.
2. **Evaluate:** Prove the candidate follows the contract and live behavior requirements.
3. **Approve:** Bind operator approval to the candidate's exact hash.
4. **Activate:** Make the approved version active for future calls.

### Core Identity

#### Name

**What:** The name the agent uses when introducing itself.

**Why:** Callers need a consistent identity.

**Use:** Choose a natural, stable name. Do not change it between calls without a business reason.

#### Role

**What:** The professional job the agent performs.

**Why:** The role tells the model what authority and expertise it should have.

**Use:** Be specific and truthful, for example “AI subscription advisor at IPTV Shopping.” Never
give the agent authority the business has not granted.

#### Organization

**What:** The company or organization represented by the agent.

**Why:** It gives the caller context and keeps company references consistent.

**Use:** Enter the public business name. Leave it blank only when the agent genuinely represents no
organization.

#### Mission

**What:** The agent's durable reason for existing.

**Why:** When instructions conflict, the mission guides the agent toward the intended outcome.

**Use:** Write one clear paragraph covering whom it helps, what it helps with, and the standard of
behavior. Do not put temporary campaign details here; those belong in Tasks.

#### English AI disclosure

**What:** The truthful English sentence used when the caller asks whether the agent is AI.

**Why:** The agent must never be instructed to deny being AI or impersonate a human.

**Use:** Keep it short and direct, for example: “I'm Adam, an AI phone representative.”

#### French AI disclosure

**What:** The same disclosure in professional French.

**Why:** Truthful identity must work in every supported language.

**Use:** Use clear French and keep the meaning equivalent to the English disclosure.

#### Values — ordered, one per line

**What:** Durable principles such as truth, respect and customer fit.

**Why:** Values guide judgment when there is no exact scripted rule.

**Use:** Put the most important value first. Use one short value per line. Avoid contradictory values.

Example:

```text
truth_over_agreement
customer_fit_before_pressure
listening_before_pitching
```

#### Decision priorities — one per line

**What:** The order used when the agent must trade one goal against another.

**Why:** “Be fast,” “be accurate,” and “close the sale” can conflict. The agent needs a declared
priority order.

**Use:** Put factual correctness and caller safety before conversion or speed.

#### Hard boundaries — optional, one per line

**What:** Actions or claims that are never allowed.

**Why:** These are the strongest identity-level safety limits.

**Use:** Leave this blank if you do not want administrator-defined hard boundaries. Otherwise write
concrete prohibitions, one per line. These are your instructions, not backend functions.

#### Forbidden behavior — optional, one per line

**What:** Communication patterns the agent must avoid.

**Why:** Some behavior is not an illegal action but still creates poor or harmful calls.

**Use:** Leave this blank if you do not want a forbidden-behavior list. Otherwise include problems
such as repeated introductions or unwanted monologues, one per line.

### Voice Identity

These settings describe conversational style. They do not select the audio voice model; that is done
under **Pipeline & S2S** or **Voice**.

#### Tone

| Choice | Meaning | Good use |
| --- | --- | --- |
| warm | Friendly and attentive | Sales and support |
| neutral | Emotionally restrained | General information |
| direct | Gets to the point quickly | Operational calls |
| calm | Reassuring and unhurried | Complaints or sensitive support |
| confident | Decisive and assured | Recommendations and closing |

#### Formality

| Choice | Meaning | Good use |
| --- | --- | --- |
| casual | Relaxed language | Informal audiences only |
| professional | Natural business language | Recommended default |
| formal | Polite, structured language | Regulated or official calls |

Professional or formal French should use `vous`, not `tu`.

#### Verbosity

| Choice | Meaning | Good use |
| --- | --- | --- |
| terse | Very short answers | Fast confirmations |
| concise | Short but complete | Recommended for calls |
| balanced | More explanation | Complex support |

Telephone calls usually work best with `concise`.

#### Pace

| Choice | Meaning | Good use |
| --- | --- | --- |
| measured | Slow and deliberate | Complex or sensitive topics |
| natural | Normal conversation | Recommended default |
| brisk | Energetic and fast | Simple, time-sensitive calls |

#### Maximum words per turn

**What:** The hard target for the number of words in one response.

**Why:** Long telephone monologues feel robotic and prevent interruption.

**Use:** `20–35` works well for most calls. The current recommended value is `30`.

#### Maximum sentences per turn

**What:** The maximum number of spoken sentences before giving the caller room.

**Why:** It encourages turn-taking and reduces stacked questions.

**Use:** `1–2` is recommended. Use `3` only when the task genuinely needs explanation.

#### Empathy

**What:** A value from `0.0` to `1.0` controlling how strongly the agent acknowledges the caller's
situation.

**Why:** Too little empathy sounds cold; too much can sound repetitive or submissive.

**Use:** Around `0.7–0.9` is appropriate for warm professional calls.

#### Assertiveness

**What:** A value from `0.0` to `1.0` controlling how confidently the agent guides the conversation.

**Why:** Too little produces vague answers; too much can become pressure.

**Use:** Around `0.6–0.8` is usually professional. Any boundaries you choose still override
assertiveness.

### Contrast Examples and Evaluation Cases

These are advanced JSON editors. A beginner should normally ask the developer to modify them.

#### Behavior examples

**What:** Examples of a situation, caller input, ideal response, bad response and explanation.

**Why:** Examples teach subtle behavior more clearly than abstract adjectives.

**Use:** Add examples for real failures you want to correct. Include English and French examples.
Keep the JSON valid.

#### Evaluation cases

**What:** Test prompts with required markers, forbidden phrases and reference responses.

**Why:** They turn persona quality into a repeatable gate instead of a subjective impression.

**Use:** Test identity, multilingual behavior, forbidden behavior, tool selection and naturalness.
Do not make tests easier merely to obtain a passing score.

### Trusted Progressive Skills

A skill is a small package of specialist instructions. It can explain how to handle a particular
type of caller need.

#### Skill enabled checkbox

**What:** Includes that trusted skill in the active Identity candidate.

**Why:** Not every agent needs every skill.

**Use:** Enable only skills relevant to this agent. The built-in phone-conversation and safe-tool-use
skills should normally remain enabled.

#### Trusted status

**What:** Confirms that the exact SHA-256 version of the skill was reviewed.

**Why:** Editing a skill changes its instructions and invalidates the old trust decision.

**Use:** Never trust a user skill merely because its name sounds safe. Review the description,
instructions and requested tools first.

#### Priority

**What:** Controls when the model receives the skill instructions.

**Why:** Loading every skill on every call makes the prompt large and less focused.

**Use:** Built-in core skills may have priority 90 or higher and remain always active. User skills in
the editor use `0–89` and load progressively when needed.

#### Create or update a user skill

##### Skill name

- What: A stable machine-friendly identifier such as `order-support`.
- Why: The model and registry need an exact name.
- Use: Use lowercase words separated by hyphens. Do not reuse an unrelated skill's name.

##### Version

- What: The skill's version, such as `1.0.0`.
- Why: Operators need to know when instructions changed.
- Use: Increase the version after a meaningful change.

##### Priority (0–89)

- What: How readily the skill should be loaded.
- Why: Lower-priority skills remain outside the main prompt until relevant.
- Use: `50` is a reasonable normal value. Do not imitate the always-on built-in priority without a
  design review.

##### Languages

- What: English, French, or both.
- Why: A language-specific skill should not be loaded in an incompatible conversation.
- Use: Choose only languages for which the instructions and examples are genuinely suitable.

##### Description

- What: A short explanation of when the skill is useful.
- Why: The model sees this description before deciding whether to load the full instructions.
- Use: State the trigger and result clearly in one or two sentences.

##### Task IDs

- What: Tasks allowed to use the skill.
- Why: An order-support skill should not automatically affect every unrelated call.
- Use: Enter one task ID per line. Blank means all tasks and should be used carefully.

##### Function tools

- What: Ordinary tool names requested by the skill.
- Why: The skill may need verified data or an action.
- Use: One exact tool name per line. Listing a tool does not grant permission; the active task must
  also allow it.

##### MCP tools

- What: Namespaced tools supplied by connected MCP servers.
- Why: It allows a specialist skill to describe relevant external capabilities.
- Use: Enter exact namespaced tool names. The hardened broker and task allowlist still control use.

##### Skill instructions

- What: The complete specialist behavior instructions.
- Why: These are loaded only when the skill is relevant.
- Use: Be concrete, truthful and concise. Never place secrets, credentials or instructions to bypass
  approval here.

##### Save Skill as Untrusted Revision

- What: Saves the authored skill without trusting it.
- Why: Authoring and security approval are intentionally separate.
- Use: Save, review the resulting exact hash, then trust it only after inspection.

##### Trust exact hash

- What: Approves one exact skill digest.
- Why: A later edit automatically becomes untrusted.
- Use: Press only after reviewing the exact content and requested tools.

### Approved Persistent Memory Blocks

Memory blocks are durable, reviewed facts or instructions compiled into calls.

#### Core self

**What:** The agent's identity summary derived from the active constitution. It is version-controlled,
not permanently locked away from you.

**Why:** The agent needs a stable understanding of itself.

**Use:** Press **Edit Core Identity**, change the name, role, organization, mission or other Core
Identity fields, then complete Stage → Evaluate → Approve → Activate. The displayed Core Self block
is not edited directly because that would bypass testing, but you remain fully able to change it
whenever needed.

#### Approved operator directives

**What:** Durable instructions explicitly approved by the operator.

**Why:** Some business instructions should persist across tasks and calls.

**Use:** Add only stable directives. Temporary campaign instructions belong in the active Task or
call-specific instructions.

#### Durable business context

**What:** Long-lived verified business context.

**Why:** The agent may need stable organizational knowledge across calls.

**Use:** Enter only verified, current facts. Product prices and changing offers are safer in a
versioned task created by Product Research.

#### Save approved block

**What:** Writes the edited mutable memory block.

**Why:** Memory changes need an explicit operator action.

**Use:** Review the complete text first. Saved blocks can affect future calls.

#### Pending inferred memory

**What:** A memory suggestion created from an explicit caller statement.

**Why:** The AI cannot silently promote its own inference into trusted persistent memory.

**Use:**

- **Approve** only when the proposal accurately reflects the evidence and is appropriate to retain.
- **Reject** when it is uncertain, unnecessary, private, outdated or wrong.

### Evaluation Report

The report contains category scores and individual findings.

| Category | What it tests |
| --- | --- |
| Identity | Name, mission, priorities and disclosure consistency |
| Multilingual | English/French examples and language behavior |
| Forbidden behavior | Deception, invention, unsafe claims and adversarial cases |
| Tool selection | Skills and tool choices stay within declared permissions |
| Naturalness | Turn length, sentence count, questions, register and robotic phrases |

`PASSED` means the contract is structurally usable and no critical data-integrity finding blocked
it. Warning scores remain advice for the administrator and do not veto approval.

`forbidden.safety_coverage` is advisory. It suggests adding verification, authorization or
anti-invention boundaries, but it does not override the administrator's chosen boundaries or block
an otherwise passing revision. The warning remains visible so you can make the final decision with
full information.

### Reason for this revision

**What:** A plain-language explanation of what changed and why.

**Why:** Future operators need to understand the revision history.

**Use:** Write a specific reason, such as “Reduce long sales monologues and require one open question
per turn.” Avoid vague text such as “improve it.”

### Revision action buttons

#### 1. Stage Revision

- What: Creates a candidate from the editor.
- Why: The live identity must remain unchanged while the candidate is tested.
- Use: Press after completing and reviewing all intended edits.

#### 2. Contract Check

- What: Runs deterministic structural and reference-response checks.
- Why: It catches broken data, invalid JSON and unavailable trusted skills before activation.
- Use: Run before approval. Warnings are advisory; you decide whether to proceed.

#### 3. Approve Exact Hash

- What: Records operator approval for the precise evaluated candidate.
- Why: Approval must become invalid if the content changes.
- Use: Press only after reading the report and confirming the candidate hash.

#### 4. Activate Next Calls

- What: Makes the approved candidate the active identity.
- Why: Activation is separated from editing and approval to prevent accidental changes.
- Use: Activate only when no call is active. The new identity applies to future calls.

---

## 5. Memory workspace

This workspace is a simple view of persistent caller records and verified preferences.

### Caller Number

**What:** The caller identifier shown according to privacy rules.

**Why:** Memories must be scoped to the correct person.

**Use:** Do not copy or share caller information unnecessarily.

### Calls

**What:** The number of stored call interactions for that caller record.

**Why:** It indicates how much historical context exists.

**Use:** A high count does not guarantee every memory is current; check status and content.

### Language

**What:** The known or preferred conversation language.

**Why:** The agent can begin future calls in the right language when the preference is approved.

**Use:** Treat it as a preference, not a permanent personal fact. The caller may switch languages.

### Status

**What:** Whether the record is active, verified or otherwise usable.

**Why:** The UI must distinguish trusted context from incomplete or unavailable data.

**Use:** Do not rely on a record whose status indicates a problem.

### Refresh Memory Store

**What:** Reloads the memory view from local storage.

**Why:** Memory writing happens asynchronously and may change after the page was opened.

**Use:** Press when you expect a recent caller record that is not yet visible. Refreshing does not
create, approve or delete memory.

---

## Part III — Giving the agent a job

## 6. Tasks workspace

An Identity is the same person across calls. A Task describes one kind of job, such as selling an
IPTV subscription, supporting a customer or booking an appointment.

### Active Task Contract

**What:** The task currently selected for the next call.

**Why:** The runtime needs exactly one objective and tool/safety contract.

**Use:** Choose the task that matches the next call. Selecting it loads its fields into the editor.

Built-in examples include:

- IPTV subscription sales;
- general customer support;
- appointment scheduling.

### Call-Specific Instructions

**What:** Temporary instructions added on top of the selected Task for the next call.

**Why:** You may need a one-time campaign detail without creating a permanent task revision.

**Use:** Include the immediate purpose, verified special context and desired treatment of the
caller. Do not repeat the complete Identity or paste secrets.

### New

**What:** Clears the task editor so you can create a new task.

**Why:** New tasks should not accidentally overwrite unrelated tasks.

**Use:** Export the current task first if you may need it later.

### Export JSON

**What:** Downloads the current task contract as a JSON file.

**Why:** It provides a portable backup and review format.

**Use:** Keep exported files private because they may contain business instructions.

### Import JSON

**What:** Loads a task contract from a JSON file.

**Why:** It supports backup restoration and reviewed task sharing.

**Use:** Import only a file you trust. Review every field before saving or selecting it.

### Delete

**What:** Deletes a user-created task contract.

**Why:** Obsolete tasks should not remain selectable forever.

**Use:** This is destructive. Export a backup first. Built-in tasks may be protected.

### Task ID

**What:** A machine-friendly identifier such as `iptv_subscription_sales`.

**Why:** Configuration, skills and calls refer to tasks by this exact ID.

**Use:** Use lowercase letters, numbers and underscores. Changing an ID creates or addresses a
different task.

### Title

**What:** A human-readable task name.

**Why:** Operators should not need to understand the machine ID.

**Use:** Use a clear business title such as “French IPTV Subscription Consultation.”

### Objective — what success looks like

**What:** One or two sentences describing the task's desired outcome.

**Why:** The model needs a clear definition of success.

**Use:** Describe a legitimate outcome, not “win at any cost.” Respect and accuracy remain higher
priorities.

### Opening Greeting — French / English

**What:** The first sentence spoken in each language.

**Why:** The opening must be consistent, tested and appropriate to the task.

**Use:** Identify the agent and company, give the reason for the call, and ask whether it is a good
time. Keep it short.

### Max spoken words

**What:** The task-specific response-length limit.

**Why:** Some tasks need stricter or looser responses than the Identity default.

**Use:** Prefer a short limit. When Identity and Task limits interact, the runtime uses the safer
constraint.

### Max sentences

**What:** The task-specific sentence limit per response.

**Why:** It prevents task instructions from producing long scripts.

**Use:** One or two sentences is recommended for normal calls.

### Information to discover

**What:** Facts the agent must learn from the caller.

**Why:** Discovery prevents irrelevant recommendations and repeated questions.

**Use:** Enter one item per line, such as viewing preferences, device count or desired callback time.
Do not request unnecessary sensitive data.

### Success criteria

**What:** Observable conditions showing that the task succeeded.

**Why:** The model and evaluation system need a measurable finish line.

**Use:** Examples include “caller selected a verified plan” or “appointment time was explicitly
confirmed.” Do not count an unverified promise as success.

### Conversation strategy — the stages

**What:** The ordered flow of the call.

**Why:** A staged strategy keeps the conversation coherent without forcing a word-for-word script.

**Use:** Use stages such as `OPEN`, `DISCOVER`, `RECOMMEND`, `HANDLE`, `CLOSE`. Describe the purpose
of each stage in plain language.

### Natural conversation rules

**What:** Task-specific speaking behavior added to the Identity.

**Why:** A support task and a sales task may need different conversational tactics.

**Use:** State rules such as one question at a time, no repeated greeting, and acknowledge only when
useful.

### Ground truth policy

**What:** The facts the task is allowed to state and the evidence required.

**Why:** It prevents invented prices, features, availability, account state and guarantees.

**Use:** Identify authoritative knowledge and require verification for anything outside it.

### Allowed tools

**What:** Exact function or MCP tool names this task may use.

**Why:** A connected tool should not automatically be usable by every task.

**Use:** Use one exact tool per line. This is one half of the permission gate; the broker must also
expose and allow the tool.

### Actions requiring approval

**What:** Consequential actions that cannot execute automatically.

**Why:** Calls may involve bookings, payments, messages or commitments that require human authority.

**Use:** Include every action that could affect money, privacy, legal position or another person.

### Stop conditions

**What:** Conditions that require the agent to end the call respectfully.

**Why:** The caller must remain in control of the conversation.

**Use:** Include clear refusal, request to stop, wrong person, unsafe situation and repeated inability
to understand.

### Save Task Contract

**What:** Validates and writes the task currently in the editor.

**Why:** Editing and persistence are separate actions.

**Use:** Press after reviewing every task field. Fix validation errors rather than weakening safety
rules.

### Save Task for Next Call

**What:** Saves the selected active task and call-specific instructions in Studio settings.

**Why:** The next call must know which task to compile.

**Use:** Press after selecting the desired task. Saved settings apply to the next call, not an active
call.

---

## 7. Product Research workspace

Product Research turns a public product website into a verified PhoneAgent task and knowledge base.
It crawls pages, extracts important facts and rejects unsupported claims.

### Product website

**What:** The public website to research.

**Why:** The engine needs source evidence for prices, features and claims.

**Use:** Enter the official product website, not an untrusted summary page.

### New task ID

**What:** The ID of the generated task.

**Why:** The result needs a stable name in the Tasks workspace.

**Use:** Use lowercase letters, digits and underscores. An existing ID may be replaced, so choose
carefully.

### Pages to crawl

**What:** The maximum number of website pages the research engine may inspect, from 1 to 60.

**Why:** More pages improve coverage but take more time and resources.

**Use:** `25` is a balanced default. Increase it for a large official documentation site; reduce it
for a small product page.

### Provider

**What:** The AI provider used to extract structured product knowledge.

**Why:** Different machines may have Codex, Antigravity, Ollama or API-backed providers available.

**Use:** Choose an available provider. An unavailable provider remains visible with a reason so it
can be repaired.

### Extraction model

**What:** The specific model used by the selected provider.

**Why:** Providers may offer multiple speed and quality choices.

**Use:** Prefer the recommended available model. A stronger model may improve extraction but take
longer.

### Activate automatically when every claim verifies

**What:** Makes the generated task active after all verification gates pass.

**Why:** It saves an extra step when the evidence is complete.

**Use:** Leave it checked only when you want successful research to become the next active task.
Uncheck it when you want to review the generated task manually first.

### Research & build task

**What:** Starts crawling, extraction, claim verification and task creation.

**Why:** The process is intentionally explicit and may take several minutes.

**Use:** Press once and monitor progress. Do not close the Studio while the job is running.

### Progress and report

**What:** Live steps, errors, evidence coverage and activation result.

**Why:** Research must be auditable rather than a hidden model call.

**Use:** Review dropped or unverifiable claims. Never manually add an unsupported price merely
because the website crawl could not verify it.

---

## Part IV — Technical runtime settings

## 8. Pipeline & S2S workspace

This is an advanced workspace. If calls already work well, leave it unchanged unless the developer
is diagnosing or testing a specific improvement.

### Voice Pipeline Architecture

#### Standard Cascade — STT → LLM → TTS

**What:** Speech is transcribed to text, an LLM writes a response, and a TTS engine speaks it.

**Why:** Each component can be selected independently, including local providers.

**Use:** Choose Cascade when you need modular STT, model and TTS selection. The **Voice** and
**Models** workspaces primarily configure this mode.

#### OpenAI Realtime S2S — Speech-to-Speech

**What:** Phone audio streams directly to OpenAI Realtime, which understands and generates speech in
one live session.

**Why:** It provides natural timing, direct barge-in and high-quality speech-to-speech conversation.

**Use:** This is the preferred mode when direct OpenAI Realtime is authenticated and qualified. Its
voice is selected here, not in the cascade TTS provider menu.

### Realtime Model

**What:** The OpenAI Realtime model used by S2S.

**Why:** The live session must declare one supported model.

**Use:** `Auto — gpt-realtime-2.1` is the recommended default. Choose the explicit model only for
controlled qualification.

### S2S Transport

#### WebSocket PCM

**What:** Raw PCM audio travels over a server-to-server WebSocket.

**Why:** It avoids a browser WebRTC/Opus round trip and matches the current phone media runtime.

**Use:** Keep the recommended WebSocket PCM option unless the developer is diagnosing compatibility.

#### WebRTC compatibility fallback

**What:** Uses a WebRTC-compatible route.

**Why:** It exists as a fallback for environments that require WebRTC behavior.

**Use:** Do not select it casually; the current direct media path is qualified around WebSocket PCM.

### Realtime Reasoning Effort

| Choice | Effect |
| --- | --- |
| Minimal | Lowest latency, least extra reasoning |
| Low | Recommended balance for telephone conversation |
| Medium | More reasoning, potentially slower replies |
| High | Most reasoning, highest latency risk |

Telephone conversation usually benefits more from fast, focused responses than long reasoning.

### Phone Turn Detection

#### Deterministic Server VAD

**What:** Detects the end of caller speech using a configured silence interval.

**Why:** Predictable turn timing is important for telephone latency and interruption behavior.

**Use:** Keep the recommended deterministic option unless real call evidence shows a problem.

#### Semantic VAD

**What:** Uses meaning and speech context to estimate whether the caller finished.

**Why:** It may handle natural pauses better in some conversations.

**Use:** It can be slower or less predictable. Test carefully before production activation.

### ChatGPT Realtime Voice

**What:** The actual OpenAI voice used in S2S mode.

**Why:** S2S generates speech directly, so it does not use the separate cascade TTS voice.

**Choices:** Alloy, Ash, Ballad, Cedar, Coral, Echo, Marin, Sage, Shimmer and Verse.

**Use:** `Marin` is the current recommended natural, expressive default. Changing a Realtime voice
should be evaluated with real listening tests because text tests cannot judge audio quality.

### Save Pipeline Settings

**What:** Persists the selected architecture and Realtime options.

**Why:** Technical changes should apply only after an explicit save.

**Use:** Save only when no call is active. The settings apply to the next call.

---

## 9. Voice workspace

The Voice workspace primarily controls **cascade TTS**. In OpenAI Realtime S2S mode, use the
Realtime Voice under **Pipeline & S2S** instead.

### TTS Provider

#### Supertonic Local

**What:** A local text-to-speech engine running on the Mac.

**Why:** It avoids a cloud TTS request and provides controllable local voices.

**Use:** Recommended for qualified local cascade calls.

#### Microsoft Edge Neural

**What:** Microsoft's online neural voices, including multilingual English/French choices.

**Why:** It provides natural fallback voices with broad language support.

**Use:** Requires network access. Andrew Multilingual is used by conversational reflexes and as a
fallback in parts of the cascade runtime.

#### Google Gemini TTS

**What:** Google's cloud TTS with scene and sample-context controls.

**Why:** It supports expressive, context-guided voice performance.

**Use:** Requires valid Google credentials and quota. Configure Scene and Sample Context carefully.

#### Kokoro-82M Studio Local

**What:** A local Kokoro speech model with language-specific voices.

**Why:** It provides another offline/local cascade option.

**Use:** The voice must match the call language. `ff_siwis` is French; `af_`, `am_`, `bf_`, and
`bm_` choices are English families.

#### VibeVoice Realtime — experimental

**What:** An experimental research voice model.

**Why:** It exists for exploration and future qualification.

**Use:** Do not use for production calls. Current French choices are exploratory, and the model may
run slower than real time.

### Microsoft Edge Voice

**What:** The selected Edge multilingual voice.

**Why:** Edge offers many voice/locale combinations that may change over time.

**Use:** Select a voice supporting the intended languages. Use **Refresh voices** to reload the live
catalog.

### Refresh voices

**What:** Requests the current Edge voice list.

**Why:** The provider catalog is dynamic and cached.

**Use:** Press when a known voice is missing or the catalog says fallback.

### Kokoro Voice

**What:** The local Kokoro speaker profile.

**Why:** Kokoro voice IDs are language-specific.

**Use:** Select French `ff_siwis` for French and an English voice for English. The backend rejects a
known language mismatch.

### VibeVoice Voice

**What:** The experimental VibeVoice speaker profile.

**Why:** It allows controlled research across available speakers.

**Use:** English is the supported path. Do not rely on exploratory French voices in production.

### Supertonic Model

#### Supertonic 3

**What:** The primary quality model.

**Why:** It prioritizes voice quality.

**Use:** Recommended when the machine meets real-time latency requirements.

#### Supertonic 2

**What:** The maximum-speed test model.

**Why:** It provides a faster comparison and fallback experiment.

**Use:** Choose only when lower latency is more important than the quality difference.

### Supertonic Voice

**What:** A speaker profile from male `M1–M5` or female `F1–F5` choices.

**Why:** The model needs a consistent speaker identity.

**Use:** Listen to and qualify the choice before production use. The current default is `M1`.

### Default Conversation Language

**What:** The default English or French language for the cascade speech configuration.

**Why:** STT and language-specific TTS need an initial language expectation.

**Use:** Choose the expected opening language. The agent may still switch naturally after a clear
caller language signal if the active pipeline supports it.

### Google TTS Model

**What:** The Google model used to synthesize speech.

**Why:** Google offers models with different streaming, latency and quota behavior.

**Use:** Prefer the streaming low-latency model; use the reliable fallback only when needed.

### Google Voice

| Voice | Character |
| --- | --- |
| Aoede | Warm female |
| Algenib | Low and gravelly |
| Algieba | Smooth, lower pitch |
| Puck | Upbeat male |

Select based on the intended business persona, then perform a listening test.

### Scene

**What:** A description of the telephone environment, relationship and emotional atmosphere.

**Why:** Gemini TTS uses context to shape its performance.

**Use:** Describe a calm professional phone call, not a radio advertisement or theatrical scene.

### Sample Context

**What:** Guidance for how the speaker enters and continues the conversation.

**Why:** It influences tone, pace, articulation and continuity.

**Use:** Keep it focused on voice delivery. It is not stored as caller memory.

### Apply Voice

**What:** Saves the cascade TTS provider, voice and language settings.

**Why:** Voice experiments should not silently affect active calls.

**Use:** Save when no call is active. Test the next call or a controlled voice sample.

---

## 10. Models workspace

This workspace primarily controls the intelligence component of the **cascade** pipeline. OpenAI
Realtime S2S uses its Realtime model selected under **Pipeline & S2S**.

### LLM Provider

#### Antigravity Gemini Bridge

**What:** A local bridge to an authenticated Gemini environment.

**Why:** It can use an existing subscription-backed model route without placing an API secret in the
WebUI.

**Use:** Choose when the bridge is authenticated and healthy.

#### Local Ollama

**What:** An LLM running locally through Ollama.

**Why:** It supports offline/private experiments and avoids cloud per-request usage.

**Use:** Ensure Ollama is running and the selected model is installed. Validate latency before calls.

#### Google Gemini API

**What:** Direct Google API access.

**Why:** It provides an API-backed Gemini route independent of the Antigravity bridge.

**Use:** Requires configured credentials and quota outside the WebUI.

#### OpenAI API

**What:** Direct OpenAI text-model access for cascade mode.

**Why:** It provides an OpenAI cascade alternative to Realtime S2S.

**Use:** Requires configured credentials. Do not confuse it with the Realtime S2S model selector.

#### OpenRouter

**What:** A gateway to supported third-party models.

**Why:** It provides provider flexibility.

**Use:** Requires OpenRouter credentials and a compatible configured model.

### Primary Intelligence

**What:** The specific text model used by the selected cascade provider.

**Why:** Model choice affects intelligence, latency, reliability and cost.

**Use:** Choose only a model served by the selected provider. Test it before production calls.

### Low-latency conversation mode

**What:** Prepares one exact response before the final pause is fully committed.

**Why:** It can reduce perceived response delay.

**Use:** Leave enabled when the qualified speculative pipeline behaves well. Disable it when
diagnosing early responses or to use the proven standard cascade sequence.

Speculative text cannot enter speech, transcript, LLM context or memory until it passes the normal
commit gate.

### Instant conversational reactions

**What:** Plays short, safe reactions while the full answer is being prepared.

**Why:** Long silence makes a telephone call feel disconnected.

**Use:** Leave enabled when using the supported Andrew reaction route. Disable it if reactions feel
repetitive or conflict with the desired style.

Reactions never enter memory or conversation context.

### Apply LLM

**What:** Saves the cascade provider, model and latency-helper choices.

**Why:** Model changes should apply only after explicit operator action.

**Use:** Save when no call is active. Validate the next controlled call before scaling use.

---

## Part V — Compatibility controls

## 11. Legacy Behavior workspace

This workspace exists because PhoneAgent had persona and repair controls before the versioned
Identity Kernel was added.

The active Identity Kernel overrides core name, role, mission and hard boundaries. Legacy settings
still influence some trait, repair and human-conversation behavior. A beginner should not edit this
workspace without developer guidance.

### Persona Name, Role & Title, Mission Statement

**What:** Older identity fields.

**Why:** They preserve compatibility with previous persona files.

**Use:** Do not use them as the authoritative identity. Make core identity changes in the Identity
workspace and follow the revision workflow.

### Analytical Intensity

**What:** How strongly the older behavior layer favors analysis and evidence.

**Why:** It helps reduce shallow or unsupported answers.

**Use:** Keep high for factual business calls, but do not let analysis create long spoken answers.

### Directness — Anti-Waffle

**What:** How strongly the agent avoids vague or unnecessarily long language.

**Why:** Telephone conversations need concise turns.

**Use:** A high value is normally helpful. The Identity word and sentence limits remain clearer
controls.

### Ambitious — Automation First

**What:** How readily the agent tries to move work forward.

**Why:** The old persona system distinguished proactive action from passive advice.

**Use:** High ambition never overrides tool allowlists, approval requirements or hard boundaries.

### Risk Tolerance

**What:** The older layer's willingness to proceed under uncertainty.

**Why:** Different tasks tolerate uncertainty differently.

**Use:** Keep moderate or low for real customer calls. It never permits guessing facts or bypassing
approval.

### Vagueness Tolerance

**What:** How willing the agent is to accept unclear information.

**Why:** Low tolerance encourages clarification instead of invention.

**Use:** Keep low. Zero does not mean interrogating the caller unnecessarily; it means asking a
focused clarification when needed.

### Decision Priority Hierarchy

**What:** The old single-line priority order separated by `>`.

**Why:** It remains compatible with the legacy prompt compiler.

**Use:** The authoritative ordered priorities now belong in Identity. Keep factual correctness first.

### Hard Non-Negotiable Boundaries

**What:** Older boundary text.

**Why:** Existing guardrails still read compatibility data.

**Use:** Never weaken it. The active Identity Kernel remains authoritative.

### Export JSON

**What:** Downloads the legacy persona configuration.

**Why:** It provides backup and review.

**Use:** Store privately.

### Import JSON

**What:** Loads a legacy persona file.

**Why:** It supports restoration and migration.

**Use:** Import only trusted, reviewed files. Imported legacy content does not bypass the Identity
Kernel.

### Save Legacy Behavior for Next Call

**What:** Saves the compatibility persona settings.

**Why:** Some old behavior controls still compile into future calls.

**Use:** Avoid changing during an active call. Test afterward.

### Human Conversation fields

These fields contain actual wording used by the prompt compiler and repair guard.

#### Presence — being a person, not an assistant

Controls conversational presence: one thought at a time, active listening and space for the caller.
It must never instruct the agent to hide that it is AI.

#### Repair principle — what to do when unsure

Defines the general rule for unclear audio. It should require clarification rather than guessing.

#### Ask again — first time

Contains varied first clarification phrases so the agent does not repeat one robotic apology.

#### Ask again — second time

Contains stronger clarification phrases, such as asking the caller to slow down or repeat only the
last part.

#### After three tries — give up politely

Defines a respectful fallback after repeated misunderstanding, such as offering a callback rather
than trapping the caller in a loop.

#### When the caller asks you to repeat

Defines the short preamble before the agent repeats its own previous point.

#### Listening — backchannels and pauses

Prevents the agent from answering every “mm-hmm,” filling every silence or interrupting natural
caller pauses.

#### Continuity — never repeat yourself

Prevents repeated introductions, repeated questions and forgetting facts already supplied in the
same call.

#### Delivery — how it speaks

Controls contractions, turn length, number pronunciation, one-question behavior and spoken register.

#### Tone and emotion

Defines warm, specific empathy without excessive apology, flattery or servility.

#### If it is a bad moment

Tells the agent to stop selling when the caller is driving, busy or unable to talk and to offer a
safe next step.

#### If asked who you are

Defines identity wording with placeholders such as `{name}` and `{company}`. It must remain truthful
about AI identity when asked.

#### Situation rules

Handles special situations such as voicemail, wrong person, loud background, closing and callback.

#### Never do this

Lists specific conversation failures observed in testing or real calls.

### Save Human Behaviour for Next Call

**What:** Saves all Human Conversation wording.

**Why:** These phrases and rules are live behavior, not documentation.

**Use:** Ask the developer to review changes. A harmless-looking sentence can produce repetition,
deception or poor turn-taking.

---

## Part VI — Safe operating recipes

## 12. Recipe: Make a normal call

1. Confirm the status says `Connected` and `IDLE`.
2. Open **Live Call**.
3. Enter and verify the full number.
4. Select the intended GSM or WhatsApp channel.
5. Leave recording unchecked unless consent and law are confirmed.
6. Press **Call** once.
7. Watch call state, transcript, playback and safety score.
8. Press **Hang Up** when the caller asks to stop or the call should end.

## 13. Recipe: Change the agent's personality safely

1. Open **Identity**.
2. Edit the smallest necessary fields.
3. Add a clear revision reason.
4. Press **Stage Revision**.
5. Run **Contract Check**.
6. Review the report; warnings are advisory.
7. Press **Approve Exact Hash**.
8. Press **Activate Next Calls**.
9. Make one controlled test call before broad use.

## 14. Recipe: Create a new call objective

1. Open **Tasks**.
2. Export the current task if it is useful as a backup.
3. Press **New**.
4. Define a unique Task ID and clear title.
5. Write the objective, greetings, discovery fields and success criteria.
6. Add verified ground-truth rules.
7. Allow only necessary tools and list consequential actions under approval.
8. Add clear stop conditions.
9. Save the Task Contract.
10. Select it as Active Task and save it for the next call.

## 15. Recipe: Research a product website

1. Open **Product Research**.
2. Enter the official product website.
3. Choose a unique task ID.
4. Keep 25 pages unless the website size requires adjustment.
5. Choose an available provider and model.
6. Uncheck automatic activation if you want manual review.
7. Start research and wait for the report.
8. Review evidence and dropped claims.
9. Open **Tasks** and inspect the generated contract before calling customers.

## 16. Recipe: Roll back a bad identity

1. Open **Identity**.
2. Press **Restore & Activate** on the desired archived version.
3. Confirm that you want that content active for subsequent calls.
4. Wait for Studio to report the newly activated version and refresh the editor fields.

The one-click action still performs a Contract Check before switching the active identity. The
archived version keeps its original number and exact hash.

---

## Part VII — Troubleshooting

## 17. Call button does not start a call

Check these items in order:

1. Is the global status `Connected`?
2. Is call state `IDLE`?
3. Is the phone number present and valid?
4. Is the selected channel available?
5. Does the Identity status say production ready?
6. Is the Android gateway connected for GSM or phone-placed WhatsApp?
7. Is direct WhatsApp paired for the Rust route?
8. Did policy or cooldown reject the destination?

Read the detailed state label below the number field before changing settings.

## 18. The caller speaks but the AI does not answer

1. Watch the audio monitor for caller audio.
2. Check whether caller transcription appears.
3. Check for a call notice or playback failure.
4. Confirm the selected pipeline and provider are authenticated.
5. Do not immediately change GSM or WhatsApp media code; determine whether the problem is input
   audio, model response or output playback.

## 19. The AI answer appears but the caller hears nothing

1. Read the playback state under the AI response.
2. `NOT HEARD` or playback failure points to output media delivery.
3. `Delivered completely` means local delivery completed; inspect route diagnostics if the remote
   side still reports silence.
4. Record only with consent if an audio artifact is needed for diagnosis.

## 20. Identity cannot be approved

Common reasons:

- the candidate has not been evaluated;
- an evaluation failed;
- the candidate changed after evaluation;
- approval does not match the exact hash.

Do not bypass the gate. Fix the candidate and repeat the workflow.

## 21. Voice option seems to have no effect

First check the pipeline:

- In **OpenAI Realtime S2S**, use **ChatGPT Realtime Voice** under Pipeline & S2S.
- In **Standard Cascade**, use the provider and voice under Voice.

Changing a cascade TTS voice does not change direct S2S speech.

## CRM & ERP workspace

This workspace connects PhoneAgent to the locally installed ERPNext, Frappe CRM and Frappe
Helpdesk suite.

- **Open CRM:** Opens leads, deals, activities and the sales pipeline.
- **Open Helpdesk:** Opens customer tickets, SLAs and knowledge articles.
- **Open ERPNext:** Opens products, stock, customers, orders, invoices, payments and accounting.
- **Activate CRM, service and ERP tools:** Makes the selected caller-bound tools available to the
  live Realtime AI.
- **Run active Frappe campaigns automatically:** Allows Studio to claim contacts from campaigns
  that an administrator already reviewed and activated in Frappe.
- **Campaign poll/claim seconds:** Controls how often Studio checks for work and how long one
  contact is reserved to this Mac. These values do not change call audio.
- **Tool checkboxes:** Control individual business abilities. Blank task IDs mean every active task.
- **Test Business Suite:** Verifies ERPNext, CRM, Helpdesk and the custom PhoneAgent API together.
- **Save & Hot Reload:** Applies tool changes to an active Realtime call within about one second.

The AI cannot choose another customer number. PhoneAgent binds every tool to the authenticated
current caller. Quotations and sales orders created during calls are drafts, not completed sales or
payments. See `docs/BUSINESS_SUITE.md` for installation, campaign and backup workflows.

## 22. A skill cannot use a tool

A skill does not grant permissions. All of these must agree:

1. the skill requests the tool;
2. the skill is trusted and enabled;
3. the active Task allows the tool;
4. the broker exposes and allows the tool;
5. any required operator approval is granted.

This deliberate double allowlist prevents a persona instruction from expanding its own authority.

## 23. Tools & MCP

This workspace connects capabilities that can become available to OpenAI Realtime while a call is
already active.

### Live Web Research

This card lets the speaking AI search the public internet during a live call. The AI knows the
tool exists, says a short “I’ll check online” sentence in the caller’s current language, searches,
reads sources and answers from the evidence. If necessary, it may refine the query, but it stops
when evidence is sufficient and can never exceed three searches for one information need. It does
not need a human approval click.

- **Activate live web research:** Makes the tool available to the Realtime AI.
- **Respect robots.txt:** Honors a website publisher’s automated-reading rules.
- **Include independent DuckDuckGo results:** Adds results from a second provider so the AI can
  compare evidence and still search when Bing is challenged. Keep this enabled for reliability.
- **Task IDs:** Blank gives every task access. Otherwise enter one allowed task ID per line.
- **Test question:** The harmless question used by **Run Real Search Test**. It is not saved as an
  agent instruction.
- **Bing result candidates:** How many search links are considered. Ten is a strong default.
- **Best pages to read:** How many top links are actually opened. Three usually gives comparison
  quality without making the caller wait too long.
- **Parallel page readers:** How many selected pages can be read at the same time.
- **Safe search:** Moderate keeps broad business results; Strict filters more aggressively.
- **Search language:** Automatic follows the request. English or French can be forced.
- **Country code:** Search region, such as `US`, `FR`, or `MA`.
- **Total deadline:** Maximum complete search time. If it expires, the AI receives an honest error.
- **Bing, page and Crawl4AI timeouts:** Smaller deadlines for each stage.
- **Characters per source / total evidence:** Limits how much webpage text reaches the model.
- **Cache lifetime / maximum cached searches:** Reuses a recent identical answer for speed.
- **Prefer domains:** Adds ranking weight to trusted domains; one hostname per line.
- **Block domains:** Prevents reading listed domains and their subdomains.
- **Use Crawl4AI:** Opens JavaScript pages in the isolated browser only if fast reading failed.
- **Crawl4AI URL:** Keep `http://127.0.0.1:11235` for the bundled local sidecar.
- **Private API token:** Installed automatically and shown masked after save.
- **Fallback timeout / pages:** Bounds browser work during one call.
- **Run Real Search Test:** Tests the current unsaved controls and visibly shows latency, every
  search card returned to the AI, source links, providers, extraction methods, character counts
  and content previews. The AI—not the tool—evaluates relevance and confidence.
- **Save & Hot Reload:** Stores the settings and updates an active call within about one second.

Recommended starting values are 10 candidates, 3 pages, 3 readers, Moderate safe search, a
9-second total deadline, 5,000 characters per source and a 10-minute cache. See
`docs/WEB_RESEARCH.md` for installation and security details.

### OpenWA live WhatsApp companion

This card gives the speaking AI controlled access to the current caller's WhatsApp chat. It does
not carry phone audio and does not change GSM or the existing direct WhatsApp call channel.

- **OpenWA server URL:** Address of the separately installed OpenWA service. Keep the bundled local
  value unless you operate a secured HTTPS server.
- **Session ID:** The linked WhatsApp session the companion uses.
- **Dedicated API key:** A key restricted to this session. The saved value is masked in Studio.
- **Request timeout:** Maximum wait for one OpenWA request.
- **Delivery confirmation wait:** How long a send tool watches for authenticated device delivery
  before returning. `3000` ms is recommended; `0` returns immediately after acceptance.
  For a same-account self-chat, PhoneAgent instead confirms that the exact message appears in chat
  history; other phone numbers continue using normal device delivery/read acknowledgements.
- **Allowed media hosts:** HTTPS hostnames from which the agent may send files. One hostname per
  line; an empty list means media sends are blocked.
- **Activate OpenWA companion:** Master switch. Leave it off until pairing and testing pass.
- **Listen for live message events:** Makes new current-caller messages and delivery ticks available
  to the Realtime conversation.
- **Respond during live calls:** Allows a matching new WhatsApp message to trigger a natural spoken
  acknowledgement. Turn it off to add context silently.
- **Activate tool:** Grants one exact current-caller capability. It never grants the other tools.
- **Admin approval:** `No approval for each use` lets the AI execute autonomously after the tool is
  activated. `Approve every use` is an optional human gate for workflows that need it.
- **Available to task IDs:** Restricts the tool to named tasks. Blank means all tasks.
- **Test connection:** Checks the server and selected session and displays a visible result.
- **Save & Hot Reload:** Applies reviewed changes to an active Realtime call within about one second.
- **Open OpenWA dashboard:** Opens the local dashboard used for QR pairing and session status.
- **One-time admin key:** Used only to list sessions or create a dedicated PhoneAgent key. It is not
  saved in PhoneAgent configuration.

The AI never chooses the recipient. PhoneAgent derives it from the current call and asks OpenWA to
confirm it. A successful send means accepted for sending, not delivered or read; only authenticated
delivery events establish those later states. See `docs/OPENWA_INTEGRATION.md` for setup.

### Add SearXNG search

Press **+ SearXNG search** to load a reviewed template for the configured SearXNG JSON endpoint.
Press **Test connection** before activation. The test uses a harmless `Berlin weather` query and
shows whether JSON results can be extracted.

### Add an HTTP tool

Use this for a fixed REST endpoint. Define the exact tool name, description, JSON input schema,
method, fixed headers, argument mapping and response list path. The model can fill only fields
declared in the schema; it cannot change the host, route, method or authorization.

### Add a Local MCP

Use a JSON argv array such as `["/absolute/path/server", "--stdio"]`. This is not a shell command.
Test the connection to discover tools, review each schema, then activate only the tools the agent
needs.

### Add a Remote MCP

Enter the Streamable HTTP MCP endpoint and any fixed authentication headers. Test discovery before
activation. Header values are hidden after save.

### Activate connection

The connection-level master switch. When off, none of its tools are available even if an
individual tool switch is on.

### Activate tool

Controls one exact tool. Turning it on is an operator decision; it does not activate sibling tools
from the same MCP server.

### Admin approval

- **No approval for each use:** the active tool may execute immediately when Realtime calls it.
- **Approve every use:** execution waits for the exact request to appear under Pending Live
  Approvals. Approve once or reject it. Expired and rejected requests never execute.

### Available to task IDs

One task ID per line. Blank means every task. Use task assignment to prevent a sales agent from
receiving an unrelated support or administrative tool.

### Save & Hot Reload

Validates and privately stores the configuration. During an active Realtime call, PhoneAgent sends
the reviewed catalog and updated tool instructions to the existing session within one second. It
does not restart the call or alter GSM or WhatsApp media.

### Plain HTTP warning

HTTP sends queries and results without transport encryption. It is rejected unless **Explicitly
allow unencrypted HTTP** is selected. Prefer HTTPS and an access token for production services.

For the complete security model and file locations, read `docs/TOOLS_AND_MCP.md`.

---

## Part VIII — Glossary

## 24. Important terms

**Active:** The version currently used by future calls.

**Approval:** Human authorization for an exact revision or consequential action.

**Barge-in:** The caller interrupts while the AI is speaking, causing playback to stop or truncate.

**Cascade:** A speech pipeline with separate STT, LLM and TTS components.

**Contract:** A structured set of requirements the runtime validates and enforces.

**Evaluation:** Automated testing of Identity behavior before activation.

**Full duplex:** Both sides can naturally speak and interrupt instead of taking rigid turns.

**GSM:** The Android phone's normal cellular call route.

**Graphiti:** An optional external long-term memory graph, used asynchronously outside live audio.

**Hash:** A unique cryptographic fingerprint of exact content.

**Identity Kernel:** The versioned system controlling identity, style, skills, reviewed memory and
evaluation.

**LLM:** The language model that decides what to say in a cascade pipeline.

**MCP:** Model Context Protocol, used to expose tools through controlled local integrations.

**PCM:** Raw digital audio samples carried by the media pipeline.

**Universal Cascade:** High performance modular voice processing chain (STT → LLM → TTS).

**Revision:** A candidate change that has not automatically replaced the active version.

**Skill:** Trusted specialist instructions loaded only when relevant.

**STT:** Speech-to-text transcription.

**Task Contract:** The objective, strategy, knowledge, tools, approvals and stop conditions for one
type of call.

**TTS:** Text-to-speech synthesis.

**VAD:** Voice activity detection, used to estimate when the caller starts and stops speaking.

---

## 24. Recommended defaults for a beginner

These are operating defaults, not universal laws:

- Identity tone: `warm`.
- Formality: `professional`.
- Verbosity: `concise`.
- Pace: `natural`.
- Maximum words: about `30`.
- Maximum sentences: `2`.
- Empathy: about `0.8–0.9`.
- Assertiveness: about `0.7–0.8`.
- Pipeline: keep the currently qualified working mode.
- Realtime reasoning: `low`.
- Realtime transport: `WebSocket PCM`.
- Realtime voice: `Marin` unless a listening test selects another voice.
- Recording: off unless consent and law are confirmed for that call.
- Built-in skills: keep `phone-conversation` and `safe-tool-use` enabled.
- Identity activation: require a Contract Check, your exact-hash approval and activation.

The safest customization method is to tell the developer the business outcome and examples of good
and bad behavior. The developer can then change the minimum necessary settings, run evaluations and
verify the next controlled call.
