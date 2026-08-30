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

Set this up from the two screens, not a terminal.

**On the computer** — Studio → **Pipeline** tab → *Remote Phone*:
tick **Accept a phone over the network**. The card shows the address and port to
type on the handset, and turns green when the phone connects.

**On the phone** — open **PhoneAgent Gateway** → *Connect to a runtime without a
cable*: type that address, switch it on, tap **Save and connect**.

Then remove the USB forwards so the relay can own the gateway ports:

```bash
adb forward --remove-all
```

That is the only command, and only because a USB forward and the relay cannot
hold ports 8765-8768 at the same time. Studio names that conflict if you forget.

### Why it works

Nothing about a call needs the cable: dial, hangup and status are HTTP on 8765,
media is three sockets on 8766-8768. The phone dials **out** — which is also how
it works behind carrier NAT, where nothing can reach in — and one connection
carries all four ports. The relay re-presents them on the computer's loopback,
the same shape `adb forward` produced, so the voice host is unchanged.

Both ends authenticate every frame with the existing PHAG link key, so no second
secret is provisioned. The handset still binds its gateway ports to loopback
only; the tunnel client runs in the same process and connects locally, and the
relay may open none of the phone's other ports.

### Limits

**Latency is the open question, not the architecture.** The media path uses
20 ms frames with a 12-frame credit window, tuned for a sub-millisecond cable.
Try it on wifi first; a wide-area link will need `UPLINK_WINDOW_FRAMES` and the
startup reservoir widened to cover the round trip.

Frames are authenticated but **not encrypted**, as on the USB path. Over
anything but a trusted LAN, run the tunnel inside a VPN.
