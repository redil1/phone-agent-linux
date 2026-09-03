# Live Tools & MCP Control Plane

PhoneAgent Studio 0.7 has a dedicated **Tools & MCP** workspace for capabilities that the
Universal Cascade agent can use during a telephone call. It is above the generic
audio transport and does not modify GSM dialing, Android media, WhatsApp signaling, or WhatsApp
media.

The workspace also contains a purpose-built **Live Web Research** tool. It uses Bing discovery,
an automatic DuckDuckGo backup, fast static reading with Trafilatura, and an isolated Crawl4AI
JavaScript fallback. Unlike a generic HTTP tool, it has public-network URL enforcement, evidence
bounds, provider diversity, caching, live diagnostics and matching Realtime persona instructions.
The tool does not decide semantic relevance or confidence; the AI evaluates the returned evidence.
See `docs/WEB_RESEARCH.md`.

## Supported connection types

### Declarative HTTP tools

An operator supplies a fixed endpoint, method, headers, bounded JSON input schema, argument
mapping, fixed parameters and response extraction path. Model arguments can become URL query
parameters or a JSON request body. The model cannot choose the host, URL, headers or method.

### Local MCP over stdio

The operator supplies a bounded argv array and optional environment-variable **names**. Commands
are started directly without a shell. Values remain in the PhoneAgent service environment and are
never copied into the browser or configuration file.

### Remote MCP over Streamable HTTP

PhoneAgent uses the official Model Context Protocol Python SDK. Fixed headers may carry the remote
server's authorization. Tool metadata is discovered from the server and must be reviewed before
individual tools are activated.

## Activation and permission model

Four conditions must agree before a managed tool reaches Realtime:

1. the connection is saved and activated;
2. the individual discovered or declared tool is activated;
3. its task assignment is blank/all or includes the active task;
4. its live schema still matches the exact schema last tested and reviewed.

Every tool has an independent **Admin approval** setting:

- **No approval for each use** executes automatically after the model calls it.
- **Approve every use** creates a private, expiring request in Studio. The external operation does
  not start until the operator approves that exact request. Rejection and expiry return an honest
  non-completion result to the model.

Activation changes are watched once per second. Both Realtime WebSocket PCM and Realtime WebRTC
sessions receive a new `session.update` containing the reviewed tool catalog and matching persona
instructions. The peer connection, cellular transport and WhatsApp transport are not restarted.
An in-flight managed-tool runtime is retired only after the call closes so a hot reload cannot
interrupt an operation that already began.

## AI-controlled call completion

`end_call` is an internal PhoneAgent control available to every Realtime call. It is not an
operator-created business tool, needs no per-use human approval, and is not configured in the
Tools & MCP screen.

The Realtime AI—not a transcript phrase matcher—judges from the complete live conversation when
the call is genuinely finished. It calls `end_call` with an internal reason and one brief closing
sentence in the caller's current language. PhoneAgent speaks that sentence once, protects it from
a second farewell overlap, waits for the authenticated phone playout acknowledgement, and then
invokes exactly one hang-up on the active channel. It must not use the control while an answer,
promised action, search, or other tool result is pending. Malformed requests do not hang up.

This control is outside the audio-critical path until the final turn. It does not alter GSM media,
the direct WhatsApp media bridge, codecs, resampling, VAD during normal conversation, or OpenWA
messaging.

## SearXNG setup

1. Open **Tools & MCP**.
2. Press **+ SearXNG search**.
3. Review the endpoint. The preset currently uses
   `http://95.217.193.163:8080/search`, maps `query` to `q`, fixes `format=json`, and extracts the
   `results` list.
4. Press **Test connection**. The default harmless query is `Berlin weather`.
5. Activate the connection and the `internet_search` tool.
6. Choose the task IDs that may use it. Blank means all tasks.
7. Choose whether every search needs a live operator approval.
8. Press **Save & Hot Reload**.

The tool description tells the agent to use internet search only for current or missing facts and
to tell the caller briefly that the online check may take a few seconds. Returned content is data,
not instructions. The model must not follow commands found in webpages.

## Security and failure behavior

- Studio remains loopback-only with Host and Origin validation.
- Tool configuration and approval records are private mode-`0600` files.
- Saved HTTP header values are masked when configuration returns to the browser.
- Plain HTTP is rejected unless the operator explicitly activates insecure transport.
- Redirects are rejected, request timeouts are bounded, response bodies are size-limited, and tool
  schemas require `additionalProperties=false`.
- Common secrets, email addresses and telephone numbers are redacted from model-visible remote
  results.
- MCP schema drift fails closed. The operator must test and review the new schema before the tool
  can return to a live catalog.
- A failed hot reload leaves the previous live catalog running and reports the error in Studio.
- Tool and approval lifecycle events enter the tamper-evident audit ledger without raw arguments
  or tool results.

## Files

- Control configuration: `~/.config/phone-agent/tool-control.json`
- Expiring approval records: `~/.local/share/phone-agent/tool-approvals/`
- Existing local Python tools: `~/.config/phone-agent/tools/`
- Legacy stdio MCP configuration remains supported at
  `~/.config/phone-agent/mcp_servers.json`.

The Studio does not accept arbitrary Python or shell source code from the browser. New executable
logic must be exposed as a reviewed HTTP endpoint, an MCP server, or an operator-installed local
Python tool. This preserves the distinction between connecting a capability and granting the web
page arbitrary code execution on the PhoneAgent host.

## OpenWA live WhatsApp companion

The same workspace includes a dedicated OpenWA card for live WhatsApp messaging with the current
caller. It is intentionally safer than connecting OpenWA's complete generic MCP catalog: Realtime
never receives a raw recipient, session identifier, host, or API key. PhoneAgent derives and
confirms the current caller's chat before each action, then exposes only the individually activated
read, send, reply, reaction, location, contact, read-state, typing, and delivery-status tools.

OpenWA is a messaging sidecar only. It does not replace, alter, or share code with GSM or the frozen
full-duplex WhatsApp voice transport. Installation, pairing, security controls, live event behavior,
and residual unofficial-WhatsApp risk are documented in `docs/OPENWA_INTEGRATION.md`.
