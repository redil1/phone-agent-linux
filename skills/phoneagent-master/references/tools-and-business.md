# Tools, Research, WhatsApp and Business Systems

## Permission intersection

A tool is usable only when all applicable conditions pass:

1. implementation/discovery succeeds;
2. its connection is enabled;
3. the individual tool is enabled;
4. the active task allows it;
5. skill/request context does not exceed that allowlist;
6. approval mode is satisfied when configured;
7. current caller/channel context is valid;
8. runtime health and timeout bounds pass.

Never solve a missing-tool error by broadly enabling everything. Fix the missing intersection.

## Built-in and user Python tools

`build_tool_catalog` supplies built-ins such as knowledge retrieval, plan lookup, callback requests,
gated actions and AI `end_call` when the task permits them. User Python tools are discovered under
`~/.config/phone-agent/tools/` through the `@realtime_tool` registry.

User tools must declare a bounded JSON schema, required fields and short timeout. They execute real
work, so their results must report only what actually happened. Import failure in one user tool is
isolated and visible; it does not stop the call.

## Managed Tools & MCP

Supported connection kinds:

- declarative HTTP GET/POST;
- local MCP over stdio;
- remote MCP over Streamable HTTP.

Each connection controls endpoint/command, fixed headers/environment names, timeout, maximum output,
discovered tools and approval timeout. Each exposed tool controls schema, enabled state, read-only
classification, approval mode and task IDs.

Security behavior:

- headers/secrets are masked in public state;
- remote HTTP requires HTTPS unless a reviewed insecure-loopback exception is set;
- redirects and oversized bodies fail;
- MCP schema drift requires a new test/review;
- stdio command/environment are constrained;
- tool results are sanitized before Realtime receives them;
- active Realtime sessions hot-reload reviewed changes.

## Exact dictated text

`ToolArgumentGrounding` addresses a real model failure where the transcript contained “Mac complete
test” but the tool argument contained “My complete test.” It grounds explicit title/description and
simple WhatsApp dictation from a bounded recent caller-turn window. It supports split requests such as
title in one turn and description in the next. Low-confidence mismatches are blocked before writing.

Compound requests are not replaced with command text. For example, “send a message saying X and also
include the ticket number” must remain a composed model action containing X plus the verified ticket
number, not the literal words “also include the ticket number.”

When adding a durable text tool, decide explicitly whether any fields require caller-literal
grounding and add realistic tests.

## OpenWA live WhatsApp companion

OpenWA is a separate messaging plane. It can, for the authenticated current caller:

- read recent chat;
- send text or allowlisted HTTPS media;
- reply/react;
- send location/contact;
- mark read/set typing;
- read the latest verified confirmation state.

The model cannot supply a phone number, JID, session ID, host or credential. PhoneAgent resolves and
confirms the current caller's chat.

Confirmation states are distinct:

- accepted: OpenWA accepted the send request;
- confirmed in chat: exact outgoing message appears in chat history (especially self-chat);
- device delivered: authenticated acknowledgement confirms delivery;
- read: authenticated acknowledgement confirms reading;
- failed: authenticated failure state.

For self-chats, “confirmed in chat” must never be called delivered/read. Live incoming messages are
marked untrusted customer content and cannot change identity, permissions or recipient.

OpenWA uses unofficial WhatsApp Web mechanisms. Use a dedicated account, pacing and lawful consent.

## Direct WhatsApp voice versus OpenWA

Direct Rust WhatsApp carries full-duplex voice and has its own linked-device database. OpenWA carries
chat messages. A healthy OpenWA session does not prove direct voice is paired, and a healthy direct
voice session does not prove OpenWA messaging tools are active.

## Live Web Research

The `web_research` tool:

1. searches Bing HTML;
2. may merge DuckDuckGo fallback discovery;
3. normalizes/deduplicates URLs;
4. reads pages through bounded static HTTP and Trafilatura;
5. uses Crawl4AI only for selected JavaScript fallbacks;
6. returns provider-labelled search results, bounded sources, dates when observed, warnings, elapsed
   time and an iteration policy.

The tool does not decide relevance, freshness, credibility, truth or next action. The Realtime AI
evaluates evidence and may search again up to the bounded information-need policy. It should announce
a short wait before a slow search and explain uncertainty when evidence remains insufficient.

Never hardcode one example's date/source filter as universal truth. The tool should expose evidence;
the AI should reason about the caller's actual information need.

Important controls include result/page limits, timeouts, language/country, safe search, source char
bounds, cache, preferred/blocked domains, robots policy and Crawl4AI limits.

## Product research

Product research is a slower offline authoring workflow, not a live call tool. It crawls a business
website, extracts structured knowledge, verifies claims, builds a task contract and optionally
activates the result. Use it to create grounded product tasks before calls.

## Frappe business suite

The Compose product installs ERPNext, Frappe CRM, Frappe Helpdesk, Telephony and the custom
`phoneagent_frappe` trust-boundary app.

The 14 caller-bound Realtime tools are:

1. `business_get_customer_context`
2. `business_upsert_current_lead`
3. `business_record_call_outcome`
4. `business_create_opportunity`
5. `business_schedule_follow_up`
6. `business_search_catalog`
7. `business_create_quotation_draft`
8. `business_create_sales_order_draft`
9. `business_get_order_status`
10. `business_get_invoice_status`
11. `business_create_support_ticket`
12. `business_get_support_status`
13. `business_update_support_ticket`
14. `business_mark_do_not_call`

PhoneAgent injects phone, call ID, task, direction and bounded item count. Phone/email values are
redacted before model exposure. Tickets also store a dedicated normalized caller-phone field so they
remain caller-bound without requiring customer/email creation.

Commercial write limits:

- quotations and sales orders remain drafts;
- a pipeline opportunity is not a completed sale;
- submitted, paid, delivered or resolved states require backend evidence;
- the AI cannot charge, submit or activate through these tools.

## Support-ticket verification

To prove a ticket is real, query Helpdesk/Frappe rather than trusting transcript text. Verify:

- ticket ID, exact subject and description;
- status, priority and timestamps;
- normalized caller association;
- lead/customer association when present;
- owner/integration user;
- linked comments/status updates;
- corresponding tool call/result and call ID.

If transcript wording differs from stored data, inspect the actual tool arguments. The database may
have faithfully stored a model argument that was already wrong.

## Campaign autopilot

Frappe stores PhoneAgent Campaign, Campaign Member, Call Log and Contact Consent documents. An
administrator activates a reviewed campaign with task, channel, timezone/window, limits, retries and
lawful-contact basis. Studio claims one eligible member only while idle, rechecks suppression/consent
and dial policy, calls, records outcome and restores its previous task/channel.

Global autopilot enablement does not activate every campaign. Do-not-call immediately suppresses
eligible pending/retry/in-progress members.

No software configuration makes unsolicited outreach universally lawful. The administrator remains
responsible for lawful basis, hours, disclosure, suppression and retention.

## Failure interpretation

- Tool unavailable: inspect connection, individual enable, task allowlist and schema drift.
- Tool says success but no record: query backend by returned ID and inspect tool result/audit.
- AI says no access: confirm tool was present when the call session started/hot-reloaded and prompt
  advertised it.
- WhatsApp accepted but not delivered: inspect confirmation state; do not resend blindly.
- Research empty: inspect discovery warnings, source access and bounded retry count.
- CRM context missing: verify normalized phone field and current-caller binding, not arbitrary lookup.
