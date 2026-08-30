# PhoneAgent Business Suite

PhoneAgent Business Suite is one Docker Compose product containing:

- ERPNext 16 for products, stock, customers, quotations, sales orders, subscriptions, invoices,
  payments, accounting, purchasing and projects;
- Frappe CRM for leads, deals, activities, follow-ups and pipeline views;
- Frappe Helpdesk for tickets, SLAs, assignment, customer portals and knowledge;
- the custom `phoneagent_frappe` trust-boundary app;
- MariaDB, Redis, workers, scheduler, WebSocket and frontend services;
- the existing digest-pinned OpenWA and Crawl4AI services.

PhoneAgent remains the AI and call runtime. It stays native on macOS because Docker Desktop cannot
own the USB Android device and because the qualified GSM/direct-WhatsApp media paths must not be
replaced. The installer hides the multi-service implementation behind one command.

## Install on macOS

Requirements:

1. Docker Desktop running.
2. `uv` and Xcode Command Line Tools for the native PhoneAgent application.
3. ADB and the qualified rooted Android phone for GSM. WhatsApp-only use does not require GSM.

Apple-silicon Macs use the bundled qualified direct-WhatsApp executable. On an Intel Mac, install
Rust through Rustup once; the installer compiles the same frozen Rust source for Intel and verifies
the resulting architecture before activation. All bundled Docker images support both architectures.

Run:

```bash
./tools/install_full_business_suite_macos.sh
```

The installer builds the custom immutable Frappe image, creates private credentials, preserves the
existing OpenWA linked-device volume, starts the full stack, migrates the site, provisions a
least-privilege PhoneAgent API user, configures all 14 business tools, runs the complete PhoneAgent
test suite, verifies the frozen WhatsApp boundary and installs the native macOS app.

URLs:

- PhoneAgent Studio: `http://127.0.0.1:8090/`
- Frappe CRM: `http://127.0.0.1:8080/crm`
- Frappe Helpdesk: `http://127.0.0.1:8080/helpdesk`
- ERPNext: `http://127.0.0.1:8080/app`

The initial ERPNext Administrator password is stored privately at
`~/.config/phone-agent/business-suite-secrets/frappe-admin`. Change it after first login and retain
the secret directory in an encrypted backup.

## Live AI capabilities

The Realtime AI receives caller-bound tools for:

- loading customer, lead, opportunity, order, invoice, subscription and support context;
- creating/updating the current caller's lead;
- recording call outcomes and next actions;
- creating opportunities and follow-up tasks;
- searching verified products, prices and stock;
- creating draft quotations and draft sales orders;
- reading order, fulfillment, invoice and payment status;
- creating, reading and updating support tickets;
- recording and enforcing do-not-call requests.

The model never supplies a destination phone number to these tools. PhoneAgent injects the
authenticated current-call number. Returned phone/email fields are redacted before reaching the
model. A quotation or sales order created on a call remains draft and cannot charge, submit,
activate or deliver anything.

## Autonomous prospecting campaigns

In ERPNext search for **PhoneAgent Campaign**:

1. Create a campaign and choose its PhoneAgent task, channel, timezone, calling window, daily
   limit, retry limit and reviewed lawful-contact basis.
2. Import **PhoneAgent Campaign Member** rows containing E.164 phone numbers and truthful consent
   status.
3. Review the list and change the campaign status to **Active**.
4. PhoneAgent Studio claims one eligible member only while idle. It re-checks calling time,
   consent, do-not-call evidence, daily limit, attempts, PhoneAgent dial policy and the one-call
   hardware lock before dialing.
5. The final disposition updates the campaign member and creates a PhoneAgent Call Log. Failed/no
   answer calls retry only within the configured bound.

Activating the global autopilot in Studio does not activate a campaign. An administrator must
still activate the reviewed campaign in Frappe. Recording remains off for autonomous calls.

## Operations

```bash
./tools/business_suite_status.sh
./tools/backup_business_suite.sh
./tools/stop_full_business_suite.sh
./tools/restore_business_suite.sh /absolute/backup/directory --confirm
```

Data lives in named Docker volumes and is not part of the image. The backup command creates an
ERPNext database/files backup and copies it to
`~/.local/share/phone-agent/business-suite-backups/`. Restore creates another recovery point before
replacing current data.

## Security and limits

- Browser-facing ports bind to `127.0.0.1` only.
- Secrets are mode `0600` files and mounted through Compose secrets where supported.
- Database and Redis are not published to the Mac network.
- Containers use health checks, restart policies, persistent volumes, PID/memory/CPU bounds and
  `no-new-privileges` where compatible.
- OpenWA remains unofficial and retains account-enforcement risk.
- No system can make sales outreach universally lawful. The administrator remains responsible for
  the campaign's lawful basis, local calling hours, disclosures, suppression lists and retention.
