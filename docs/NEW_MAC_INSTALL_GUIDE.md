# PhoneAgent — Complete New Mac Installation Guide

This guide explains how to install the complete PhoneAgent system on a clean Mac and verify it
end to end. It is written for an administrator who does not need to understand the source code.

The installation includes:

- PhoneAgent Studio and the native Android USB/audio bridge;
- OpenAI Realtime speech-to-speech;
- GSM inbound and outbound calling;
- direct full-duplex WhatsApp voice calling;
- OpenWA live WhatsApp messaging;
- live web research with Bing, the fast reader and Crawl4AI;
- Frappe CRM, ERPNext and Frappe Helpdesk;
- autonomous, consent-aware prospecting campaigns;
- persistent storage, backups, restore and rollback.

## 1. Understand the installation

The product has two coordinated parts:

1. **The Docker Compose business suite** runs CRM, ERP, Helpdesk, OpenWA, web research, databases
   and background workers.
2. **The native PhoneAgent service** handles the Android USB device and qualified call audio. It
   must remain native because Docker Desktop cannot reliably own the Android USB media device.

You still install and operate both parts with one installer. Do not replace this structure with one
literal container: doing so would weaken the qualified GSM and WhatsApp media paths.

## 2. What you need

Before starting, have:

- a recent Mac with an internet connection;
- an administrator account on the Mac;
- Docker Desktop;
- Xcode Command Line Tools;
- `uv`;
- Android Platform Tools (`adb`) for GSM;
- the complete `phone_agent_gateway` project folder;
- the qualified rooted Android phone and a reliable USB data cable;
- access to the OpenAI/Codex account used by PhoneAgent;
- access to the WhatsApp account that will be linked.

The bundled Docker images support Apple silicon and Intel Macs. Apple-silicon Macs use the bundled
qualified direct-WhatsApp executable. An Intel Mac also needs Rustup so the installer can compile
the same frozen Rust source for Intel.

## 3. Install the Mac prerequisites

### 3.1 Install Docker Desktop

Install Docker Desktop for the Mac's processor. Open Docker Desktop and wait until the Docker engine
reports that it is running.

Verify it in Terminal:

```bash
docker info
```

If this command reports that it cannot connect to Docker, open Docker Desktop and wait before
continuing.

### 3.2 Install Xcode Command Line Tools

Open Terminal and run:

```bash
xcode-select --install
```

Complete the macOS installation dialog. Verify it:

```bash
swiftc --version
```

### 3.3 Install Homebrew, uv and ADB

Install Homebrew if it is not already available. Then run:

```bash
brew install uv android-platform-tools
```

Verify both commands:

```bash
uv --version
adb version
```

### 3.4 Intel Macs only

On an Intel Mac, install Rustup once:

```bash
brew install rustup
rustup-init
```

Close and reopen Terminal after `rustup-init`, then verify:

```bash
cargo --version
```

This step is not needed on Apple silicon when using the bundled qualified executable.

### 3.5 Sign in to OpenAI/Codex

Install or open Codex on the new Mac and sign in to the OpenAI account used for PhoneAgent. Do not
copy access tokens through email or chat. PhoneAgent reads the authenticated local Codex session
and refreshes it locally.

You can confirm that the local authentication file exists without displaying its contents:

```bash
test -f ~/.codex/auth.json && echo "Codex authentication is available"
```

## 4. Copy the project to the new Mac

Copy the complete project directory to the new Mac. A recommended location is:

```text
~/Desktop/PhoneAgent/phone_agent_gateway
```

Do not copy only the Docker Compose file, the `.app`, or the `tools` directory. The installer needs
the complete source tree, lock file, Android qualification data and frozen WhatsApp manifest.

Open Terminal and enter the project directory:

```bash
cd ~/Desktop/PhoneAgent/phone_agent_gateway
```

If the copy process removed executable permissions, restore only the installer script permissions:

```bash
chmod 755 tools/*.sh integrations/business_suite/scripts/*.sh
```

## 5. Prepare the Android phone for GSM

Skip this section only when the installation will never use GSM.

1. Connect the already-qualified rooted Android phone with a USB data cable.
2. Unlock the phone.
3. Enable USB debugging if it is not already enabled.
4. Accept the **Allow USB debugging** prompt for the new Mac.
5. Select **Always allow from this computer** only if this is a trusted Mac.

Check the connection:

```bash
adb devices
```

Expected result:

```text
List of devices attached
YOUR_DEVICE_SERIAL    device
```

If the result says `unauthorized`, unlock the phone and accept the authorization prompt. If no
device appears, check the cable, USB mode and Android developer settings.

The existing qualified Android PhoneAgent service must already be installed on the phone. A
different phone model or Android build requires device qualification before production calling.

## 6. Run the complete installer

Make sure Docker Desktop is running, then execute:

```bash
cd ~/Desktop/PhoneAgent/phone_agent_gateway
./tools/install_full_business_suite_macos.sh
```

Do not run the installer with `sudo`.

During the first installation, the installer will:

1. validate the source and frozen WhatsApp boundary;
2. create private local credentials;
3. build the custom Frappe business image;
4. start MariaDB, Redis, workers, CRM, ERPNext and Helpdesk;
5. start OpenWA and Crawl4AI;
6. create and migrate the Frappe site;
7. provision the least-privilege PhoneAgent integration account;
8. activate all 14 caller-bound business tools;
9. run the complete automated test suite;
10. build, sign and install the native PhoneAgent application;
11. start PhoneAgent automatically through macOS `launchd`;
12. verify that the local services are healthy.

The first run can take approximately 15–40 minutes depending on the Mac and internet connection.
Do not close Terminal or stop Docker while it is running.

Successful installation ends with:

```text
PhoneAgent Business Suite is installed and healthy.
```

The native application is installed at:

```text
~/Applications/PhoneAgent.app
```

PhoneAgent will also start automatically after future Mac logins.

## 7. Open PhoneAgent and the business applications

Open these addresses on the same Mac:

- PhoneAgent Studio: `http://127.0.0.1:8090/`
- Frappe CRM: `http://127.0.0.1:8080/crm`
- Frappe Helpdesk: `http://127.0.0.1:8080/helpdesk`
- ERPNext: `http://127.0.0.1:8080/app`
- OpenWA dashboard: `http://127.0.0.1:2785`

You can also open the native application from `~/Applications/PhoneAgent.app`.

### 7.1 First ERPNext login

The username is:

```text
Administrator
```

Display the generated initial password locally in Terminal:

```bash
cat ~/.config/phone-agent/business-suite-secrets/frappe-admin
```

Do not send this password to another person. Log in and change it from the administrator account
settings. Keep the secret directory in an encrypted backup.

## 8. Pair direct WhatsApp voice calling

Direct WhatsApp voice calling uses the frozen Rust media pipeline. It is separate from OpenWA
messaging.

1. Open PhoneAgent Studio.
2. Open **Live Call**.
3. Select **WhatsApp · Direct Rust media** as the call channel.
4. Enter the WhatsApp account phone number in international format.
5. Select or enter the correct country code.
6. Start the pairing process.
7. Enter the generated pairing code in WhatsApp under **Linked devices**, or use the displayed QR
   workflow when available.
8. Wait until PhoneAgent reports that the direct WhatsApp session is paired and ready.

The linked session is stored locally under:

```text
~/.local/share/phone-agent/whatsapp-rust.db
```

Do not delete this file unless you intentionally want to pair again.

## 9. Pair the OpenWA messaging companion

OpenWA lets the AI send and read WhatsApp messages for the authenticated current caller during a
live call. It does not carry WhatsApp call audio.

### 9.1 Open the dashboard

Open:

```text
http://127.0.0.1:2785
```

If the dashboard asks for the OpenWA administrator key, display it locally with:

```bash
sed -n 's/^OPENWA_MASTER_KEY=//p' ~/.config/phone-agent/business-suite.env
```

Paste it only into the local OpenWA dashboard. Never send it through chat, email or a screenshot.

### 9.2 Link WhatsApp

1. Create or open the session named `phoneagent-ai`.
2. Request a QR code.
3. On the iPhone, open WhatsApp → **Settings/You → Linked devices → Link a device**.
4. Scan the QR code.
5. Wait until OpenWA reports that the session is `ready`.
6. Seeing **Google Chrome (OpenWA)** in WhatsApp's linked-device list is expected.

### 9.3 Connect OpenWA to PhoneAgent

1. In PhoneAgent Studio, open **Tools & MCP**.
2. Expand **OpenWA · Live WhatsApp Companion**.
3. Keep the server URL as `http://127.0.0.1:2785`.
4. Use the one-time administrator key to load sessions.
5. Select `phoneagent-ai`.
6. Create the dedicated PhoneAgent key.
7. Press **Test connection**.
8. Confirm that the server is reachable and the session is ready.
9. Activate the WhatsApp tools needed by the agent.
10. Keep their approval mode at autonomous/no approval if the AI should operate independently.
11. Enable **Respond during live calls** if incoming messages should re-enter the live spoken
    conversation.
12. Enable **Activate OpenWA companion**.
13. Press **Save & Hot Reload**.

The AI cannot choose an arbitrary recipient. PhoneAgent binds these tools to the authenticated
number participating in the current call.

## 10. Verify CRM, ERP and Helpdesk

In PhoneAgent Studio:

1. Open **CRM & ERP**.
2. Confirm **Activate business tools** is enabled.
3. Confirm the connection URL is `http://127.0.0.1:8080`.
4. Press **Test connection**.
5. Confirm Frappe, CRM, ERPNext, Helpdesk, Telephony and `phoneagent_frappe` are ready.
6. Confirm the 14 business tools are enabled.
7. Press **Save & Hot Reload** if any setting was changed.

The AI can now securely use the current caller's CRM context, create or update leads, create
opportunities, schedule follow-ups, search products, create draft quotations/orders, check
orders/invoices, manage support tickets and record do-not-call requests.

Quotations and sales orders remain drafts. The AI cannot submit documents, charge a customer or
claim payment/delivery without verified backend evidence.

## 11. Verify live web research

In **Tools & MCP**:

1. Expand **Live Web Research · Bing + Fast Reader + Crawl4AI**.
2. Confirm it is enabled.
3. Confirm Crawl4AI uses `http://127.0.0.1:11235`.
4. Press its connection test.
5. Confirm Crawl4AI is reachable.
6. Save and hot reload if anything changed.

The AI decides what the search evidence means. The search tool returns bounded search results and
page content; it does not make the business decision for the AI.

## 12. Verify GSM and inbound answering

From the project directory run:

```bash
uv run phone-agent-qualify --ensure-forwards
```

This validates the connected Android profile and recreates required ADB forwarding when safe.

In PhoneAgent Studio:

1. Open **Live Call**.
2. Select **GSM phone**.
3. Enable **AI answers incoming GSM calls**.
4. Confirm the global status is **Connected**.
5. Confirm the call state is **IDLE**.
6. Confirm the incoming receptionist state is **Listening**.

When no outbound call is active, the Android phone now waits for an incoming call, answers it and
connects the caller to the Realtime AI.

## 13. Run the system status check

Run:

```bash
cd ~/Desktop/PhoneAgent/phone_agent_gateway
./tools/business_suite_status.sh
```

Expected services include healthy Frappe backend/frontend, MariaDB, Redis, OpenWA and Crawl4AI,
plus running workers, scheduler and WebSocket services.

Open PhoneAgent Studio and confirm:

- **Connected**;
- call state **IDLE**;
- inbound receptionist **Listening**;
- CRM/ERP connection ready;
- OpenWA server and session ready;
- Crawl4AI reachable.

## 14. Perform a safe end-to-end call test

Use your own authorized test number. Do not call an unrelated person during qualification.

### 14.1 Inbound test

1. Call the Android GSM number from your test phone.
2. Confirm the phone answers automatically.
3. Confirm the opening greeting is heard promptly.
4. Speak naturally and interrupt the AI once to test full duplex.

Ask the AI:

```text
What is the phone number calling you?
```

It should state the authenticated current-call number.

Then ask:

```text
Search the internet and tell me today's date. Give me two sources.
```

The AI should briefly ask you to wait, run live research, evaluate the results and answer with
sources.

Then ask:

```text
Create a support ticket saying new Mac installation test successful.
```

Verify that the ticket appears in Frappe Helpdesk.

Then ask:

```text
Send me a WhatsApp message saying PhoneAgent new Mac test successful.
```

Verify the message in WhatsApp. Server acceptance, confirmation in chat, device delivery and read
status are different states; the AI should report only the state actually verified.

Finally say:

```text
Thank you, goodbye.
```

The AI should respond naturally and use its hang-up tool to end the call.

### 14.2 Outbound test

1. In **Live Call**, select the intended channel.
2. Enter only your authorized test number in international format.
3. Confirm the active task and identity.
4. Start the call.
5. Verify two-way audio, interruption handling, tools and AI-controlled hang-up.

### 14.3 Verify the business result

After the call, check:

- the PhoneAgent transcript and diagnostics;
- the CRM lead/customer context;
- the PhoneAgent Call Log;
- the support ticket;
- the WhatsApp message and its exact confirmation state.

## 15. Create the first backup

After successful testing, run:

```bash
./tools/backup_business_suite.sh
```

Backups are stored under:

```text
~/.local/share/phone-agent/business-suite-backups/
```

Keep copies on encrypted storage. A Docker image is not a backup of the database or WhatsApp
pairing data.

## 16. Everyday operation

Docker Desktop must be running for CRM, ERP, Helpdesk, OpenWA and Crawl4AI. PhoneAgent itself starts
automatically after login.

Useful commands:

```bash
# Show the business-suite status
./tools/business_suite_status.sh

# Create a backup
./tools/backup_business_suite.sh

# Stop the Docker business suite without deleting data
./tools/stop_full_business_suite.sh

# Restore a reviewed backup
./tools/restore_business_suite.sh /absolute/path/to/backup --confirm

# Roll back the native PhoneAgent installation
./tools/rollback_macos.sh
```

Stopping the stack does not delete its named volumes. Do not delete Docker volumes unless permanent
loss of CRM/ERP data and OpenWA pairing is intended.

## 17. Moving existing data from the old Mac

For a completely fresh installation, pair WhatsApp again and start with an empty CRM.

If existing business data is required:

1. Run `./tools/backup_business_suite.sh` on the old Mac.
2. Copy the resulting backup directory through encrypted storage.
3. Install PhoneAgent normally on the new Mac first.
4. Copy the backup directory to the new Mac.
5. Stop active calls.
6. Run the restore command with the absolute copied backup path and `--confirm`.
7. Reopen CRM/ERP and verify the data.

Identity, tool configuration and memory live under `~/.config/phone-agent/` and
`~/.local/share/phone-agent/`. Copy them only through encrypted storage and only while PhoneAgent is
stopped. Never publish these folders because they can contain credentials and customer data.

For safety, pair direct WhatsApp and OpenWA again on the new Mac instead of casually copying session
databases or Docker volumes.

## 18. Common problems

### Docker is not running

Symptom:

```text
Cannot connect to the Docker daemon
```

Fix: open Docker Desktop, wait for it to finish starting, then rerun the installer. The installer is
idempotent and safely reuses completed work and persistent data.

### Port 8080, 8090, 2785 or 11235 is already used

Close the unrelated application using that port. Then rerun the installer. Do not delete the
PhoneAgent Docker volumes.

### ADB says unauthorized

Unlock the Android phone, reconnect USB and accept the Mac's debugging key. Then run:

```bash
adb devices
```

### PhoneAgent opens but Realtime is not authenticated

Open Codex and sign in on the same macOS user account. Confirm `~/.codex/auth.json` exists without
printing its contents, then restart PhoneAgent.

### Incoming calls keep ringing

Confirm:

- the Android phone is connected and authorized;
- no outbound call is active;
- Studio says **Connected** and **IDLE**;
- **AI answers incoming GSM calls** is enabled;
- the inbound receptionist says **Listening**.

Run the device qualification command again if needed:

```bash
uv run phone-agent-qualify --ensure-forwards
```

### OpenWA shows no QR code

Confirm the OpenWA container is healthy with `./tools/business_suite_status.sh`. Open or restart the
`phoneagent-ai` session in the local dashboard and request a fresh QR code. Also remove a stale
failed linked-device entry from WhatsApp before retrying when necessary.

### The AI says it cannot use CRM, WhatsApp or web search

Open the relevant section in PhoneAgent Studio, test the connection, activate the master switch and
required individual tools, then press **Save & Hot Reload**. Start a new call if the active call was
created before the capability became ready.

### The installer fails during activation

The native installer automatically restores the previous working PhoneAgent snapshot when its
health check fails. Read the final Terminal error, correct the stated requirement and rerun the same
installer.

## 19. Security reminders

- Keep every service bound to `127.0.0.1` unless a reviewed remote deployment is intentionally
  designed.
- Never publish `~/.config/phone-agent/` or paste its secrets into chat.
- Use a dedicated business WhatsApp number for unofficial automation.
- OpenWA is unofficial and can carry WhatsApp account-enforcement risk.
- Respect consent, do-not-call requests, local calling hours and required disclosures.
- Activate outbound campaigns only after reviewing their contacts and lawful basis.
- The AI may operate autonomously after campaign activation, but the administrator remains
  responsible for business policy and legal compliance.
- Keep quotations and orders as drafts until the business intentionally reviews and submits them.

## 20. Installation completion checklist

The new Mac is ready only when every item below is true:

- [ ] Docker Desktop is running.
- [ ] The full installer completed without an error.
- [ ] PhoneAgent Studio opens at port 8090.
- [ ] Studio shows **Connected** and **IDLE**.
- [ ] The inbound receptionist shows **Listening**.
- [ ] The Android phone appears as `device` in `adb devices`.
- [ ] OpenAI/Codex authentication is available.
- [ ] Frappe CRM, ERPNext and Helpdesk open correctly.
- [ ] All 14 PhoneAgent business tools are available.
- [ ] Crawl4AI is reachable.
- [ ] Direct WhatsApp voice is paired if it will be used.
- [ ] OpenWA `phoneagent-ai` is paired and ready if messaging will be used.
- [ ] An authorized inbound test call completed with two-way audio.
- [ ] Live internet research worked during the call.
- [ ] A test Helpdesk ticket was created.
- [ ] A test WhatsApp message was confirmed correctly.
- [ ] The AI ended the call through its hang-up tool.
- [ ] The first business-suite backup completed successfully.

When all items pass, the new Mac installation is ready for controlled production use with the
qualified Android device.
