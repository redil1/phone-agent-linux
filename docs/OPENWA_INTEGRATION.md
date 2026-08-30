# OpenWA Live WhatsApp Companion

PhoneAgent uses OpenWA as an optional messaging companion for a live telephone call. The Realtime
AI can read and interact with the current caller's WhatsApp chat while continuing the spoken
conversation. OpenWA does **not** carry call audio, place WhatsApp voice calls, or answer WhatsApp
voice calls. The existing frozen Rust WhatsApp voice pipeline continues to carry full-duplex call
audio, and the GSM pipeline is unchanged.

## What the agent can do

The operator may independently activate these current-caller tools:

- read recent messages;
- send text, approved HTTPS media, a location, or a contact card;
- reply to a specific message or react to it;
- mark the current chat read or show a typing indicator;
- check verified delivery updates received during the call.

The model never supplies a phone number, WhatsApp JID, OpenWA session ID, host, or API key. For
every operation PhoneAgent derives the recipient from the authenticated call context, asks OpenWA
to confirm that number is a WhatsApp contact, and then uses the confirmed chat ID. A tool therefore
cannot be redirected to a different customer by prompt injection or an invented argument.

An accepted send is not proof of delivery or reading. The agent may claim delivery/read status only
after a matching authenticated `message.ack` event arrives from OpenWA. Send tools now wait for a
short, operator-configured interval for device delivery or reading. An acknowledgement that races
ahead of the HTTP send response is retained and matched safely. The wait stops at its deadline, so
WhatsApp can never block the live conversation indefinitely.

When the caller number is exactly the linked OpenWA account number, WhatsApp treats the operation
as a self-chat and may never emit device-delivered/read acknowledgements. PhoneAgent detects that
exact equality from authenticated session metadata and verifies the outgoing message ID in the
same WhatsApp chat history instead. The AI may say **confirmed in the WhatsApp chat**, but never
mislabel that as device-delivered or read. This fallback never runs for a different caller number.

## Install and pair

1. Start Docker Desktop.
2. Run `tools/install_openwa_sidecar.sh` from the project directory.
3. Open `http://127.0.0.1:2785` on this Mac.
4. Create or open the `phoneagent-ai` session and scan its QR code in WhatsApp under **Linked
   Devices**.
5. In PhoneAgent Studio, open **Tools & MCP → OpenWA live WhatsApp companion**.
6. Enter the one-time OpenWA admin key from
   `~/.config/phone-agent/openwa-sidecar.env`, load sessions, select the paired session, and press
   **Create dedicated PhoneAgent key**. The admin key is used only for this request and is never
   saved by PhoneAgent.
7. Press **Test connection**. Confirm both the server and selected session are ready.
8. Activate only the individual tools needed by the active task. This installation defaults to
   autonomous execution after activation; **Approve every use** remains available when a future
   workflow intentionally needs a human gate.
9. Turn on **Activate OpenWA companion**, then press **Save & Hot Reload**.

Pairing is intentionally a human action. PhoneAgent cannot scan or approve a WhatsApp linked-device
QR code on the account owner's behalf.

## Behavior during a call

At call start, PhoneAgent exposes only the activated tools that also match the active task ID. If
the configuration changes during a Realtime call, both WebSocket PCM and WebRTC sessions receive an
updated tool catalog without restarting the call.

When live events are enabled, the sidecar subscribes to authenticated Socket.IO events. It accepts
only non-group, non-self messages whose sender matches the current caller. Duplicate message IDs
are ignored. For outgoing messages, server acceptance, device delivery, reading and failure remain
separate states. The AI receives the exact verified state after the bounded confirmation wait.

The AI also receives the authenticated phone number for the current call as routing metadata. It
may state, repeat or send that number only when the caller explicitly requests it or when needed
for an action the caller requested; it must never announce the number unsolicited.
are ignored. Incoming text is explicitly marked as untrusted customer content before it is added to
the Realtime conversation, so it cannot change identity, permissions, recipients, or security
policy. When **Respond during live calls** is enabled, the AI acknowledges the new message naturally
in speech; otherwise the context is available without triggering a new spoken turn.

Initial connection failures and later disconnects retry with bounded exponential backoff. A failed
hot reload leaves the prior catalog available long enough for in-flight work to complete and shows
the error in Studio.

## Security model

- The bundled OpenWA service listens only on `127.0.0.1` and is pinned to an exact image version and
  digest. It uses OpenWA's browser-free Baileys engine and a stable node identity across restarts,
  avoiding Chromium/Web-build compatibility failures. Linked sessions start automatically after a
  sidecar restart.
- The container has a read-only root filesystem, dropped capabilities, bounded resources, a health
  check, persistent session storage, message pacing, and a local-only dashboard.
- The OpenWA master key and key pepper live in a mode-`0600` sidecar environment file.
- PhoneAgent stores only a dedicated operator key restricted to one OpenWA session. Its private
  configuration is mode `0600`; the browser receives a mask, never the key.
- Remote OpenWA endpoints require HTTPS. HTTP is accepted only for localhost.
- HTTP redirects, oversized responses, unknown configuration fields, invalid schemas, and
  incomplete activation fail closed.
- Media must use HTTPS and match an operator-maintained hostname allowlist.
- Tools run autonomously once individually activated. An operator can still opt a tool into an
  exact, expiring per-use approval; OpenWA receives nothing before that optional approval.
- Audit records contain lifecycle metadata and hashed identifiers, not message content, tool
  results, phone numbers, or credentials.

## Operations

- Start or upgrade the pinned sidecar: `tools/install_openwa_sidecar.sh`
- Stop it without deleting paired-session data: `tools/stop_openwa_sidecar.sh`
- Sidecar definition: `~/.local/share/phone-agent/openwa/compose.yaml`
- Sidecar secrets: `~/.config/phone-agent/openwa-sidecar.env`
- PhoneAgent companion configuration: `~/.config/phone-agent/openwa.json`
- Persistent OpenWA data: Docker volume `phoneagent-openwa-data`

Stopping the sidecar does not unlink the phone or delete the Docker volume. Never delete that volume
unless loss of the paired OpenWA session and stored OpenWA data is intended.

## Important residual risk

OpenWA uses unofficial WhatsApp Web mechanisms. It may stop working after a WhatsApp change, and
WhatsApp may restrict or ban the linked account. Use a dedicated business number, start with low
volume, retain OpenWA's pacing controls, obtain any legally required consent, and avoid unsolicited
bulk messaging. This integration does not make unofficial automation supported by WhatsApp.
