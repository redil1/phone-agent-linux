# Agent Configuration and Behavior

## Precedence

Effective behavior is compiled approximately in this order:

1. Protected execution/security invariants.
2. Active Identity Kernel constitution and approved memory.
3. Active task contract and call direction context.
4. Trusted skill instructions whose task/language scope matches.
5. Current-caller memory and verified business context.
6. Runtime/call-specific additional instructions.
7. Tool results and fresh external evidence.
8. Legacy persona/human-conversation compatibility wording where still applicable.

A lower layer cannot grant authority denied by a higher layer. Tool availability is always the
intersection of implementation, activation, task allowlist, skill request and caller binding.

## Identity Kernel

The active `IdentityProfile` contains:

- `identity_id` and monotonically managed version;
- core name, role, mission, organization and truthful AI disclosures;
- ordered values and decision priorities;
- optional administrator hard boundaries, forbidden behavior and topics;
- voice style: tone, formality, verbosity, empathy, assertiveness, humor, pace, word/sentence limits,
  one-question and code-switching behavior;
- supported/default languages (currently English and French);
- contrast examples and evaluation cases;
- enabled skill IDs.

Identity changes use immutable revisions and hashes. The normal lifecycle is draft → evaluated →
approved → activated. AgentPackage activation executes this lifecycle under the authenticated
external-agent actor. Activation is blocked during a call and rejects stale identity bases.

Core self memory is derived from the active identity and is immutable through the memory API. Change
the name, role or mission through an identity revision, not by editing `core_self`.

## Behavior examples and evaluation cases

Behavior examples teach style by contrast:

- situation and caller input;
- preferred response;
- response to avoid;
- rationale, language, tags and optional expected skill.

Evaluation cases are replayable contract evidence for identity, multilingual behavior, forbidden
behavior, tool selection and naturalness. Warnings inform the administrator. Critical contract
failures include deceptive identity instructions, missing truthful disclosures, unavailable trusted
skills and invalid required multilingual coverage.

Do not optimize a persona merely to maximize the evaluator score. Real call evidence is superior.

## Task contracts

`TaskEngine` validates bounded YAML/JSON contracts. A task may define:

- `id`, title and objective;
- English/French opening greetings;
- spoken word/sentence limits;
- `inputs_required`: simple slot IDs or objects with question and detection patterns;
- success criteria;
- conversation strategy and natural-conversation rules;
- ground-truth policy;
- allowed tools and approval-required actions;
- stop conditions;
- bounded knowledge facts;
- multilingual sample phrases keyed by situation;
- objection playbook with objection, answer and source.

Task contracts are the job specification, not a fixed script. The model should adapt naturally while
collecting evidence and respecting stop conditions.

Use the task that matches the call. Running a support test under a sales task can produce an
irrelevant `abandoned` sales outcome even when the support action succeeded.

## Task runtime

`TaskRuntime` tracks slots, stage, outcome and collected evidence. Detection patterns help avoid
asking the same question twice; they do not replace the model's semantic understanding. A slot should
represent information genuinely needed for success, not every possible conversation detail.

The outcome is a structured task result. Verify it independently from durable tool outcomes. A ticket
can be created successfully while the active sales task remains unqualified.

## Call context

`CallContextPolicy` prevents a generic sales script from treating every call identically.

Outbound cold prospecting progresses through:

- disclose/unexpected-call framing;
- permission;
- relevance discovery;
- problem/demand development;
- qualification only after interest/relevance;
- proposal and close.

Inbound calls start intent-led. The caller already initiated contact, so the agent should discover
the request and may move into relevant qualification/support without cold-call permission wording.

This layer can block premature qualification and replace it with a safe discovery question. It must
not decide whether the customer is interested without evidence.

## Skills

A `SkillDraft` defines:

- stable name, semantic version and description;
- detailed instructions;
- ordinary and MCP tool names it may use;
- task IDs and languages;
- priority from 0–89 for user skills.

Trusted high-priority built-ins may be always on. Lower-priority skills use progressive disclosure:
the model sees a catalog and calls `load_agent_skill` only when relevant. Loading a skill reveals
instructions but does not grant tools.

User skill bytes are digest-trusted. Editing the file changes its digest and removes effective trust
until the new digest is explicitly trusted or deployed through an authenticated AgentPackage.

Use a skill for reusable specialist procedure, not one call's objective or one customer fact.

## Memory

There are two complementary systems:

1. Layered caller memory used by the conversation runtime for caller history and turn summaries.
2. Identity memory blocks/episodes for stable approved context and asynchronous long-term mirroring.

Memory block kinds are self, human, business, procedural and episodic index. Blocks carry source,
confidence, priority, validity dates and optional hashed caller scope.

External AgentPackages may replace only mutable non-self blocks. Agent-inferred memory must enter as
a proposal with evidence and be approved/rejected. Never store secrets, payment data or unsupported
inferences as memory.

## Knowledge

Small stable task facts belong in `task.knowledge`. Larger/fresh information should come from product
research, Frappe or live web research. The agent may state prices, availability, fulfillment and
policy only from authoritative configured sources or verified tool results.

Product research can crawl a product website, extract facts, verify numeric claims, build a task and
optionally activate it. Treat generated contracts as evidence-backed candidates, not permission to
invent missing commercial facts.

## Runtime control

The AgentPackage `RuntimeControl` exposes safe between-call parameters:

- cascade versus direct Realtime;
- GSM, Android WhatsApp or direct Rust WhatsApp channel;
- STT/LLM/TTS provider, model, language, voice and aggregation;
- speculative low-latency and conversational-reflex switches;
- auto-answer and WhatsApp country code;
- Realtime model/voice/transport/reasoning/transcription languages;
- noise reduction, VAD mode/eagerness/silence, idle re-engagement and speech speed;
- bounded additional system prompt.

`ProviderConfig.validate()` remains authoritative for compatible combinations. Prefer previously
qualified profiles. Raw audio sample rates, frame durations, protocol ports, codecs and Android mixer
controls are not AgentPackage fields.

## Legacy behavior editor

The legacy persona and `human_conversation.yaml` still influence compatibility wording and repair
behavior. They are not the authoritative identity. New name/role/mission/value changes belong in the
Identity Kernel. Avoid maintaining conflicting instructions in both systems.

## Designing a strong agent for a new job

1. Define the measurable objective and stop conditions.
2. Choose truthful authority and identity.
3. Identify caller context: inbound intent or outbound prospect.
4. Define minimal required slots and stage progression.
5. Add verified business facts and source policy.
6. Add realistic objections and multilingual examples.
7. Add specialist skills only when reusable.
8. Activate only tools the job actually needs.
9. Configure a qualified voice/latency profile.
10. Add evaluation scenarios covering success, refusal, ambiguity, multilingual turns, tool failure,
    exact dictated text and natural ending.
11. Validate, stage, read the diff, activate and qualify with authorized calls.

## Anti-patterns

- A giant system prompt containing identity, catalog, CRM data and procedural code.
- One task reused for sales, support, booking and collections.
- Skills that claim they grant tools or permissions.
- Global memory containing one caller's information.
- Tool descriptions that promise success without backend confirmation.
- Hardcoded phrases that force robotic repetition.
- Lowering VAD silence until hesitant speech becomes multiple turns.
- Changing framework/media code to express a business behavior available in AgentPackage.
