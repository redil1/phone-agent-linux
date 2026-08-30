# PhoneAgent Call Context Strategy

PhoneAgent automatically distinguishes who initiated every call and applies a different
conversation strategy before the product Task Contract is allowed to drive qualification.

## Why this layer exists

An outbound prospect saying “Yes” to “Is now a good time?” has granted permission to hear one more
sentence. They have not expressed interest in IPTV, selected a product, or agreed that they have a
problem. Treating that “Yes” as interest caused the agent to jump immediately to device and package
questions.

An inbound caller is different: initiating the call is already an intent signal. The agent should
confirm the reason and move directly into relevant help or qualification.

This distinction is independent of GSM or WhatsApp. Direction comes from call ownership:

- a PhoneAgent dial request creates an `outbound` context;
- a call received by the long-running voice host creates an `inbound` context.

## Outbound cold-prospecting stages

1. `await_permission`: deliver the opening once and wait for permission.
2. `relevance_discovery`: ask how the person currently handles the relevant area.
3. `need_development`: uncover what they would improve, avoid, simplify, save, or gain.
4. `interest_check`: connect the person's own situation to one verified outcome and ask whether it
   is worth exploring.
5. `product_qualification`: only after explicit interest may product slots such as device, package,
   budget, channels, duration or setup become the next question.
6. `close`: a refusal or lack of interest ends selling immediately.

Permission never unlocks product qualification. Explicit interest does.

## Inbound intent-led stages

Inbound calls start in `intent_discovery` with product qualification unlocked. The agent:

1. confirms why the caller contacted the business;
2. identifies the desired outcome;
3. moves directly into the relevant Task Contract;
4. keeps all normal truth, tool, approval and stop-condition rules.

It does not force an outbound permission script onto someone who already called for help.

When Studio auto-answer is enabled, a persistent GSM voice host detects `RINGING`, answers through
the authenticated Android gateway, attaches two-way media and starts this inbound intent-led AI
session. Its greeting welcomes the caller and asks how it can help; it never speaks the outbound
“I am calling about…” opening.

## Demand creation

Demand creation is ethical need development, not manipulation. The agent may:

- ask about the person's current situation;
- discover friction, cost, unreliability, inconvenience, missing content or desired outcomes;
- reflect the caller's own words;
- connect the stated situation to one verified useful outcome;
- ask whether that outcome is worth exploring.

The agent may not invent a problem, use fake urgency, pressure the person, or treat politeness as
buying intent.

## Deterministic enforcement

The policy is not prompt-only:

- a provider-neutral state machine tracks direction, prospecting phase and explicit interest;
- the live state hides product task slots until interest is unlocked;
- cascade output replaces a premature device/package/budget question with the correct relevance or
  need-development question;
- Realtime and cascade base prompts receive the same direction contract;
- call-context events expose direction and phase to Studio diagnostics.

The task still tracks its required slots in the background. They become eligible only after the
prospecting gate opens.

## Example

### Wrong cold-call sequence

> Agent: Is now a good time for a quick chat?
>
> Prospect: Yes.
>
> Agent: Which device do you plan to watch on?

The final question assumes interest that the person never expressed.

### Correct cold-call sequence

> Agent: I know this call is unexpected—is now a bad time for one quick question?
>
> Prospect: No, go ahead.
>
> Agent: How are you currently watching the TV or sports that matter most to you?
>
> Prospect: Mostly separate apps, and live matches are unreliable.
>
> Agent: So reliability and having it in one place would matter most. Would a simpler option with
> stable live access be worth exploring?
>
> Prospect: Yes, tell me more.
>
> Agent: Which device would you mainly watch on?

The device question is now relevant because interest is explicit.

## Verification

Regression tests prove that:

- “Yes” unlocks relevance discovery, not product qualification;
- substantive context advances need development;
- a value/interest check precedes product questions;
- explicit interest unlocks task slots;
- refusal closes the sales path;
- the Realtime AI owns the semantic decision to call `end_call`, while PhoneAgent waits for the
  final sentence to be rendered before executing one channel hang-up;
- inbound calls remain direct;
- the known premature device-question example is replaced before cascade speech;
- GSM and frozen WhatsApp transports are not changed by this policy layer.
