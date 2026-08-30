# PhoneAgent Identity Kernel

The Identity Kernel is a provider-neutral control layer around the existing call runtime. It does
not own dialing, Android audio, WhatsApp signaling, codecs, OpenAI audio streaming, VAD, playback,
or barge-in. Both cascade and direct OpenAI Realtime continue through their existing transports;
the kernel contributes only approved instructions, memory context, progressive skill metadata,
and one read-only skill-loading tool.

## Architecture

```text
active identity constitution (immutable)
        + approved memory blocks
        + trusted skill catalog
        + active task contract and tool allowlist
        v
PersonaCompiler
        v
existing cascade prompt or direct OpenAI Realtime session instructions

completed verified turns
        v
bounded background queue -> private SQLite -> optional Graphiti REST mirror
```

The active profile lives at `~/.config/phone-agent/identity/active.json`. Memory blocks, revisions,
skill trust records, review proposals, historical profiles, and a hash-chained identity audit are
kept in the same mode-`0700` directory with mode-`0600` files. Local long-term episodes live under
`~/.local/share/phone-agent/identity-memory.sqlite3`.

## Constitution

The versioned constitution defines name, role, mission, organization, truthful AI disclosure,
ordered values, decision priorities, hard boundaries, forbidden behavior, supported languages,
voice style, multilingual contrast examples, evaluation cases, and enabled skills. Unknown fields
are rejected. The active file has no direct update API.

The Studio lifecycle is deliberately separate:

1. **Stage** creates a candidate bound to the exact active-profile hash.
2. **Contract evaluation** checks identity completeness, multilingual coverage, deception,
   skill bindings and phone-natural reference responses. Non-critical findings are advisory and do
   not override the administrator's decision.
3. **Approve** binds the operator to the exact candidate hash.
4. **Activate** refuses while a call is running, refuses a stale base profile, archives the old
   profile, synchronizes the immutable self block, and affects only subsequent calls.

Legacy persona files are migrated once. Directives that instruct the agent to impersonate a human,
deny AI status, or “laugh off” an AI question are removed during bootstrap and are critical
evaluation failures if reintroduced.

## Memory

The version-controlled `core_self` block is derived from the active constitution. An operator can
edit it whenever needed through a new Core Identity revision and the normal evaluation, approval
and activation workflow; the derived block itself is protected from direct edits so that it cannot
diverge from the tested constitution. Mutable business and
procedural blocks can be edited by the local operator. An `agent_inferred` block cannot be written
directly: it becomes a proposal with evidence and affects no call until approved. Caller-scoped
blocks use a one-way scope hash. When `PHONE_AGENT_IDENTITY_PROPOSALS_ENABLED=true`, the validated
memory writer can propose an explicitly stated English/French language preference; duplicate
pending proposals collapse to one review item. Once approved, only that caller's prompt receives
the block.

Completed English/French turns are written after response delivery through a bounded background
worker. The worker first commits to private local SQLite. If `PHONE_AGENT_GRAPHITI_URL` is set, it
then mirrors the episode through Graphiti's `/messages` contract. Graphiti failure never blocks,
delays, ends, or changes a live call. Search used on the call path remains local and bounded.
Plain HTTP is accepted only on explicit loopback. External HTTPS Graphiti hosts must also appear in
`PHONE_AGENT_GRAPHITI_ALLOWED_HOSTS`, preventing an arbitrary configured URL from receiving the
Graphiti bearer token.

## Skills and tools

Skills use a strict `SKILL.md` contract. Built-ins are package-trusted. User skills are disabled
until the Studio trusts their exact SHA-256; any later file change automatically removes trust.
Symlinked directories/resources, path escapes, oversized files, unsupported metadata, invalid tool
names and user attempts to override a built-in skill fail closed.

Priority 90–100 skills are always compiled. Lower-priority skills expose only their name and
description until Realtime calls `load_agent_skill`. Loading returns instructions and resources;
it never grants tool permission. The task contract remains the authority for function tools, and
the existing MCP broker still requires both its server allowlist and the active task allowlist.
Skill scripts are intentionally not executed by the Identity Kernel.

## Operations

- Configure paths and Graphiti in `.env.example`.
- Use the **Identity Kernel** Studio tab to create and activate revisions.
- Back up the entire identity directory and SQLite database together.
- Never restore only `active.json` without its history and audit ledger.
- Run `uv run pytest -q` and `uv run python tools/verify_frozen_whatsapp.py` after upgrades.
