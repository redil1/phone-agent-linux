# Installing PhoneAgent on a new Mac

```bash
git clone https://github.com/redil1/PhoneAgent.git
cd PhoneAgent
./tools/bootstrap_macos.sh
```

That installs the locked Python environment, verifies the frozen WhatsApp
boundary, runs lint and the full suite, builds and code-signs the macOS app, and
installs the Studio LaunchAgent. It is safe to re-run; the installer keeps a
rollback snapshot of the previous install.

Studio then runs at <http://127.0.0.1:8090>.

## What the machine must have

| Requirement | Why | Enforced |
|---|---|---|
| **Apple Silicon** | Kokoro and Parakeet run on MLX/Metal, which has no Intel build | bootstrap refuses to continue |
| **macOS 14+** | reviewed baseline | warns |
| **Xcode Command Line Tools** | `swiftc` builds the desktop app | bootstrap fails with the fix |
| **uv** | locked dependency resolution | installed automatically if absent |

Optional, each gating one feature rather than the app:

| Component | Gates |
|---|---|
| `adb` (Android platform-tools) | GSM calls |
| Antigravity.app, signed in | zero-key Gemini LLM and live STT |
| Docker | Frappe CRM/ERP, OpenWA, crawl4ai |
| `cargo` (Rust) | direct WhatsApp voice sidecar |

About **2.9 GB** of model weights (Parakeet 2.3 GB, Kokoro 339 MB, Supertonic
256 MB) download on first use, not at install time.

## What a clone cannot bring with it

Cloning gives you the software. Three things are bound to hardware or to an
account and have to be established on the new machine:

**A rooted Android handset.** The GSM path captures and injects in-call audio
through a privileged system service. Without a paired phone the Studio runs and
the pipeline works, but no call can be placed. The WhatsApp voice channel is an
alternative that needs no handset.

**The shared link key.** `~/.config/phone-agent/link.key` authenticates every
media frame between Mac and phone. It is deliberately not in the repository.
Generate and provision it with `./android_service_apk/provision_link_key.sh`,
which writes both sides at once.

**Provider access.** With Antigravity signed in, no API key is needed for the
LLM. Everything else reads `~/.config/phone-agent/secrets.env` (mode 0600,
`NAME=value` per line). The runtime merges that file at startup, so a key put
there survives upgrades — unlike one placed in the LaunchAgent plist, which the
installer rebuilds on every install.

## Phone setup

```bash
./android_service_apk/build_and_install.sh            # build and sign the APK
./android_service_apk/install_privileged.sh --commit  # install into the live overlay
./android_service_apk/provision_link_key.sh           # shared PHAG v1 link key
uv run phone-agent-qualify --ensure-forwards          # formal readiness report
```

The privileged install writes into a `/system` overlay that **Android discards
on reboot**, restoring whatever APK is baked into the system image. For an
install that survives reboots, bake it in:

```bash
./android_service_apk/build_persistent_gsi.sh \
    --base-image /path/to/pristine/system.img \
    --output artifacts/persistent-gsi/system-phoneagent.img

./android_service_apk/flash_persistent_gsi.sh --serial <SERIAL> \
    --image artifacts/persistent-gsi/system-phoneagent.img \
    --rollback-image /path/to/pristine/system.img \
    --link-key ~/.config/phone-agent/link.key --commit
```

The flash script refuses to run unless the device is the reviewed userdebug GSI
with verification disabled, the image size matches the partition exactly, no
call is in progress, and the battery is above 50%. It requires an untouched
rollback image and prints the recovery command if the flash fails.

## Verifying an install

```bash
uv run pytest -q                                    # full suite
curl -s http://127.0.0.1:8090/api/status            # Studio
curl -s http://127.0.0.1:8765/health                # Android gateway
uv run python -m phone_agent_gateway.mac_client.provider_preflight
```

The preflight runs a complete STT → LLM → TTS turn offline, with no phone and no
call, and reports real-time factors per provider.

## Rolling back

```bash
./tools/rollback_macos.sh
```

Restores the previous app, runtime and LaunchAgent from the snapshot taken
before the last install. Identity data, Studio settings, audit logs and
recordings live in user-scoped directories and are never overwritten by an
upgrade.

## Running the phone without a cable

The runtime reaches the handset through four TCP ports that `adb forward`
tunnels over USB. Nothing about a call needs the cable: dial, hangup, answer and
status are HTTP on 8765, and media is three sockets on 8766-8768. Replacing the
transport therefore lets the runtime live on another machine entirely.

A handset on mobile data sits behind carrier NAT, so the phone dials **out** and
the runtime multiplexes the four ports back down that one socket. The relay
re-presents them on its own loopback, which is the same shape `adb forward`
produced, so **the voice host needs no change** — it still talks to
127.0.0.1:8765-8768.

On the runtime machine:

```bash
PHONE_AGENT_REMOTE_LINK=true PHONE_AGENT_REMOTE_LINK_PORT=8770 uv run phone-agent-web
```

On the handset, write `remote-link.json` into the gateway's private files
directory and restart the service:

```bash
adb shell "su -c 'cat > /data/user/0/com.phoneagent.gateway/files/remote-link.json <<JSON
{\"enabled\": true, \"host\": \"YOUR_RUNTIME_HOST\", \"port\": 8770}
JSON'"
adb shell am force-stop com.phoneagent.gateway
adb shell am start-foreground-service -n com.phoneagent.gateway/.GatewayService
```

Both ends authenticate every frame with the existing PHAG link key, so no new
secret is provisioned. Studio reports the tunnel under `remote_link` in
`/api/status`, including round-trip time.

The handset keeps binding its gateway ports to loopback only. The tunnel client
runs inside the phone process and connects to them locally, so nothing on the
phone is exposed to the network, and the relay may only ask for the four gateway
ports — never another local service.

**The open question is latency, not architecture.** The media path uses 20 ms
frames with a 12-frame credit window, tuned for a sub-millisecond cable. Test on
a LAN first; a wide-area link will need `UPLINK_WINDOW_FRAMES` and the startup
reservoir widened to cover the round-trip time.

Frames are authenticated but **not encrypted**. Over anything other than a
trusted LAN, run the tunnel inside a VPN.
