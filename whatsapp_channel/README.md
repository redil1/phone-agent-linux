# PhoneAgent WhatsApp-Rust channel

The production direct-WhatsApp path is a free, self-hosted companion client
built on [`whatsapp-rust`](https://github.com/oxidezap/whatsapp-rust). It is a
separate channel from GSM: it does not use ADB, Android Telecom, PHAG, the
cellular modem, or the rooted phone's audio routes.

## Architecture

```text
PhoneAgent AI pipeline (20 ms PCM)
        ↕
ai_bridge/whatsapp_link.py
  - 20 ms ↔ 60 ms rechunking
  - generation-aware flush
  - bounded/paced output
        ↕ framed stdin / raw PCM stdout
whatsapp-rust-caller
  - persistent linked-device SQLite session
  - WhatsApp call signaling and peer-answer detection
  - MLow/Opus negotiation
  - RTP/SRTP/SFrame/WARP relay media
  - native terminate/hangup
        ↕
WhatsApp peer
```

The older Go/meowcaller and Safari utilities remain under `native_caller/`,
`native_caller.py`, and `whatsapp_caller.py` for research and compatibility;
the Studio and voice host resolve `whatsapp-rust-caller` by default.

## Build

The sidecar pins an exact audited `whatsapp-rust` revision. Rust is selected by
`rust-toolchain.toml`.

```bash
cd whatsapp_channel/rust_caller
./build.sh
```

The release binary is copied to:

```text
whatsapp_channel/whatsapp-rust-caller
```

Override it when needed with:

```bash
export PHONE_AGENT_WHATSAPP_BINARY=/absolute/path/to/whatsapp-rust-caller
```

## Pair once

The Studio exposes the pairing flow, or use the binary directly:

```bash
./whatsapp_channel/whatsapp-rust-caller pair-phone 0600000000 --country-code 212
```

Enter the displayed code in:

```text
WhatsApp → Linked Devices → Link a Device → Link with phone number instead
```

The persistent encrypted-session material is stored by default at:

```text
~/.local/share/phone-agent/whatsapp-rust.db
```

Override the database location with:

```bash
export PHONE_AGENT_WHATSAPP_SESSION_DB=/secure/path/whatsapp-rust.db
```

Check pairing without connecting to a call:

```bash
./whatsapp_channel/whatsapp-rust-caller status
```

## Run from PhoneAgent

Choose `WhatsApp — direct Rust media (two-way)` in Studio, or set:

```bash
export PHONE_AGENT_CALL_CHANNEL=whatsapp
```

The ordinary `phone-agent-voice --dial NUMBER` command then starts the Rust
sidecar and waits for an authoritative WhatsApp `<accept>` before attaching the
AI greeting.

## Media and interruption contract

- Peer and agent audio: PCM s16le, 16 kHz, mono.
- PhoneAgent frames: 20 ms / 640 bytes.
- WhatsApp-Rust frames: 60 ms / 1920 bytes.
- Python-to-Rust frames carry generation and sequence identities.
- Barge-in clears the Python queue, clears the Rust source queue, and advances
  generation so old pipe frames are rejected.
- The Rust source queue is two 60 ms frames; Python output is paced at 60 ms.
- A transport acknowledgement means the Rust media source accepted the frame.
  It does not claim that the remote physical speaker rendered it.

## Verification

```bash
cd whatsapp_channel/rust_caller
cargo fmt --all -- --check
cargo test
cargo clippy --all-targets -- -D warnings

cd ../..
uv run pytest -q tests/test_whatsapp_link.py
uv run pytest -q
uv run ruff check ai_bridge mac_client tests
```

`PHONE_AGENT_WHATSAPP_RUST_MOCK=1` enables a deterministic local call loopback
used only by the cross-language test suite. It never contacts WhatsApp.

## Operational limits

This is an unofficial companion-client implementation. WhatsApp can change its
protocol or restrict linked accounts. Use a dedicated account, pin revisions,
rate-limit outbound calls, and run controlled live-call gates before upgrades.
No unofficial implementation can prove remote speaker rendering; RTCP and
media diagnostics provide network-level evidence instead.

