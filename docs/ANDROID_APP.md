# Building and installing the Android gateway

The handset runs a privileged system service that captures and injects in-call
audio. Changing it means building an APK and putting it on the phone yourself:
there is no Play Store path, and the app cannot work as an ordinary user app
because the permissions it needs are only granted to `/system/priv-app`.

Read the [reboot trap](#the-reboot-trap) before choosing an install route. It is
the single thing most likely to waste an afternoon.

## Build

```bash
./android_service_apk/build_and_install.sh --build-only
```

Produces `android_service_apk/PhoneAgentGateway.apk`. By default, development
builds use the local debug keystore that the script generates on first run. An
upgrade of an installed privileged app must use the exact same signing key as
the APK already on the phone:

```bash
PHONE_AGENT_SIGNING_KEYSTORE=/secure/path/phoneagent.keystore \
PHONE_AGENT_SIGNING_ALIAS=debug \
PHONE_AGENT_SIGNING_STORE_PASSWORD='...' \
PHONE_AGENT_SIGNING_KEY_PASSWORD='...' \
./android_service_apk/build_and_install.sh --device-id SERIAL
```

The installer compares certificate digests before changing Android and fails
closed on a mismatch. The same command works on Linux and macOS; set
`ANDROID_HOME` or `ANDROID_SDK_ROOT` when the SDK is not in the platform's
default location. Keep the signing keystore outside source control and backups
under access control; losing it makes in-place upgrades impossible.

Omit `--build-only` and it also runs `adb install -r -g`. This is a production
update only when the same package is already baked into the system image as a
privileged app; Android then keeps the system package's privileged identity and
stores the newer APK as an updated-system-app. On a stock phone it is only a
normal user app and cannot touch call audio.

### What the build needs

| | |
|---|---|
| Android SDK | `$ANDROID_HOME`, `$ANDROID_SDK_ROOT`, `~/Android/Sdk` (Linux), or `~/Library/Android/sdk` (macOS) |
| Build tools | `34.0.0` (`aapt2`, `d8`, `zipalign`, `apksigner`) |
| Platform | `android-34` (`android.jar`) |
| JDK | any recent `javac` and `keytool` |

There is no Gradle. The script runs `aapt2` → `javac` → `d8` → `zipalign` →
`apksigner` directly, which keeps the build readable and dependency-free at the
cost of doing the wiring by hand. Third-party jars go in `android_service_apk/libs/`
and are picked up automatically by both `javac` and `d8`; the QR decoder
(`zxing-core-3.5.3.jar`) is vendored there rather than fetched at build time so
the APK is reproducible offline.

Minimum API is 28.

## Supported phones

The source is portable; the cellular audio route is not universal. A usable
GSM handset must provide all of the following:

- a rooted or `userdebug` Android build where the APK can be installed as
  `/system/priv-app` with the included privileged-permission allowlist;
- the default-dialer role and the protected telephony/audio permissions listed
  in `AndroidManifest.xml`;
- an audio HAL/policy that exposes `AudioDeviceInfo.TYPE_TELEPHONY` for in-call
  capture and injection;
- a working `su` implementation. The verified MTK/GSI configuration uses
  `phh-su`; the installer grants only the command-scoped
  `killall audioserver` recovery capability.

The current hardware-qualified target is Android 14 `userdebug` on the reviewed
MTK/GSI phone. A different Android version, vendor audio HAL, or stock locked
phone must be qualified before relying on it for calls. Installing the APK alone
cannot add a telephony audio route that the vendor firmware does not expose.

### Tests that do not need a phone

```bash
./android_service_apk/test_protocol_codec.sh   # media framing vs the runtime
./android_service_apk/test_remote_link.sh      # tunnel framing vs the runtime
```

Both compare Java output against a golden vector shared with the Python tests.
A byte-order or field-width disagreement would authenticate on neither side and
strand every call, which is not something you would find by reading the code.

## The reboot trap

`/system` on this device is a read-only image. `install_privileged.sh` writes
into an **overlay** that Android mounts over it — and **discards on every
reboot**, restoring whatever APK is baked into the image.

That failure is silent and convincing: `pm path` still reports the right
location, `ROLE_DIALER` is still held, permissions are still granted, and the
service still answers `/health`. Everything looks installed while the phone runs
old code. It once cost a full debugging session before a hash comparison showed
the running APK was five days old.

Two consequences:

- After **any** phone reboot, an overlay install is gone. Re-run it.
- `install_privileged.sh` now verifies the installed APK **by hash** and fails
  loudly rather than reporting success against a stale build.

Always confirm with bytes, never with `pm path`:

```bash
adb shell md5sum /system/priv-app/PhoneAgentGateway/PhoneAgentGateway.apk
md5 -q android_service_apk/PhoneAgentGateway.apk
```

## Route A — overlay install (temporary)

```bash
./android_service_apk/install_privileged.sh --commit
```

Remounts `/system` read-write, pushes the APK and the privileged-permission
allowlist, restarts the Android framework, re-grants runtime permissions and
`ROLE_DIALER`, then verifies the hash.

Use it for a quick iteration you are about to replace. Be aware that the
framework restart (`stop` / `start`) has been observed to leave `zygote` in a
restart loop on this device; the only recovery is a reboot, which then discards
the overlay. If that happens twice, use Route B instead of retrying.

## Route A2 — update an already-baked privileged app (safe iteration)

When PhoneAgent is already present in `/system/priv-app` and the new APK is
signed with the same key, update it without modifying `/system` or restarting
the framework:

```bash
./android_service_apk/build_and_install.sh
```

Verify that `dumpsys package com.phoneagent.gateway` still reports the
`SYSTEM`/`PRIVILEGED` flags and protected permissions. This update survives a
normal reboot, but Android's **Uninstall updates** action or a factory reset
restores the APK baked into the image. Use Route B to make the new version the
factory baseline.

## Route B — bake into the system image (durable, recommended)

This is the reliable path. The APK goes **inside** the image, so it survives
reboots because there is no overlay to lose.

**1. Build the image** — the pristine base is never modified:

```bash
./android_service_apk/build_persistent_gsi.sh \
    --base-image /path/to/pristine/system.img \
    --output artifacts/persistent-gsi/system-phoneagent-$(date +%Y%m%d-%H%M%S).img
```

It clones the base, injects the APK and allowlist with `debugfs`, fixes
ownership, mode and the SELinux label, then verifies the embedded bytes by hash
and runs `e2fsck`. Requires Homebrew `e2fsprogs`.

**2. Flash it:**

```bash
./android_service_apk/flash_persistent_gsi.sh \
    --serial <SERIAL> \
    --image  <the image you just built> \
    --rollback-image /path/to/pristine/system.img \
    --link-key ~/.config/phone-agent/link.key \
    --commit
```

Only `system_a` (or `_b`) is written. Afterwards it re-provisions the dialer
role, permissions and the link key, then verifies everything.

### What the flash refuses to do

It stops rather than proceeding if any of these is untrue, because each one
turns a routine flash into a brick:

- fingerprint contains `tdgsi_arm64_ab`
- `ro.build.type` is `userdebug` and `ro.boot.verifiedbootstate` is `orange`
- AVB verification is disabled
- image size matches the partition **exactly**
- Telecom reports no call in progress
- battery is at least 50%
- a rollback image is supplied and the same size

If the flash fails it prints the exact `fastboot` command to restore the
rollback image.

## Verifying an install

```bash
adb shell md5sum /system/priv-app/PhoneAgentGateway/PhoneAgentGateway.apk
adb shell pm path com.phoneagent.gateway
adb shell cmd role get-role-holders android.app.role.DIALER 0
adb shell dumpsys package com.phoneagent.gateway | grep -E 'CAPTURE_AUDIO_OUTPUT|MODIFY_AUDIO_ROUTING|MODIFY_PHONE_STATE'
curl -s http://127.0.0.1:8765/health
```

The hash is the only one of these that can distinguish a fresh install from a
reverted overlay.

Prove persistence properly by rebooting and checking the hash again:

```bash
adb reboot && adb wait-for-device
adb shell md5sum /system/priv-app/PhoneAgentGateway/PhoneAgentGateway.apk
```

## Pairing the handset

The shared link key authenticates both the USB media protocol and the remote
tunnel, so the two sides disagreeing breaks everything at once and silently.

Scan it rather than moving it by hand: Studio → **Pipeline → Remote Phone →
Show pairing QR**, then on the phone **Connect to a runtime without a cable →
Scan pairing code from Studio**. One code carries the key, the address and the
port together, because a phone correctly keyed but pointed at the wrong host
fails exactly as silently as a mismatched key. Both screens then show the same
short key id; check they match.

A phone holds one key, so it pairs with one runtime at a time. Pointing it at a
different machine means scanning that machine's code.

The older `provision_link_key.sh` still works over USB and writes both sides at
once.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Everything reports installed but behaviour is old | overlay discarded by a reboot — compare hashes |
| `zygote` restarting, `system_server` absent after an overlay install | framework restart wedged; reboot, then use Route B |
| Caller hears silence after repeated failed attaches | inspect `/audio/status`; the host and APK permit only one physical Telephony-TX attempt, and the APK restarts `audioserver` after call teardown on the qualified phh-su image |
| `adb devices` shows `offline`, or `error: closed` | USB link; replug the cable, then `adb reconnect offline` |
| Phone connects to the tunnel but dialling fails | key mismatch — re-pair by QR |
| Tunnel never connects, phone retries forever | port blocked. **A timeout is a firewall; an instant close is authentication.** |
| `could not forward local 8765` while the tunnel is up | expected — the relay owns those ports and the link now proceeds anyway |
