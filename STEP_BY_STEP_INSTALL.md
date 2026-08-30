# 🚀 PhoneAgent Linux: Step-by-Step Installation & Operations Guide

Complete end-to-end installation guide for **PhoneAgent Linux** — the autonomous cellular AI voice gateway with GPU-accelerated speech models, Frappe CRM/ERPNext, WhatsApp automation (OpenWA), and full MCP autopilot control.

---

## 📋 System Prerequisites

- **OS**: Ubuntu 22.04 LTS or Ubuntu 24.04 LTS (x86_64)
- **GPU**: NVIDIA GPU with CUDA support (RTX 3060+, RTX 4090, A100/H100/L40, etc.)
- **NVIDIA Drivers**: Version 535+ installed (`nvidia-smi` working)
- **Disk Space**: At least 30 GB free space
- **Network Ports**: `8090` (Studio), `8080` (Frappe CRM), `2785` (OpenWA), `8770` (Handset Tunnel)

---

## ⚡ Quick Start (3 Commands)

```bash
# Clone the repository
git clone https://github.com/redil1/phone-agent-linux.git
cd phone-agent-linux

# 1. Bootstrap host environment & system tools
./tools/bootstrap_linux.sh

# 2. Deploy core PhoneAgent & GPU AI runtime
./deploy.sh

# 3. Launch Frappe CRM, ERPNext, Helpdesk & OpenWA WhatsApp
./tools/install_full_business_suite_linux.sh
```

---

## 📖 Detailed Step-by-Step Breakdown

### Step 1: Clone the Repository

```bash
git clone https://github.com/redil1/phone-agent-linux.git
cd phone-agent-linux
```

---

### Step 2: Bootstrap Host Environment (`./tools/bootstrap_linux.sh`)

This script sets up all system-level requirements on your Ubuntu host:

```bash
./tools/bootstrap_linux.sh
```

**What this step does automatically:**
- Updates `apt` packages and installs build tools (`build-essential`, `curl`, `git`, `ffmpeg`, `sox`, `libasound2-dev`).
- Installs and configures **Docker** and **NVIDIA Container Toolkit** (`nvidia-ctk`) so Docker containers have direct GPU acceleration.
- Installs Python 3.11 development libraries and `uv` package manager.
- Configures required directory structures in `~/.config/phone-agent` and `~/.local/share/phone-agent`.

---

### Step 3: Deploy Core PhoneAgent & GPU AI Runtime (`./deploy.sh`)

This step builds and launches the core PhoneAgent voice engine container:

```bash
./deploy.sh
```

**What this step does automatically:**
- Builds the `Dockerfile.cuda` container image with CUDA-accelerated PyTorch, Pipecat, and audio runtimes.
- Downloads and prewarms GPU speech models:
  - **TTS**: Kokoro-82M (super-fast, ultra-natural neural speech synthesis) & Supertonic.
  - **STT**: SenseVoice Small (low-latency streaming speech-to-text with auto language detection).
- Starts the `phoneagent-core` container in production mode.
- Exposes **PhoneAgent Studio** on `http://localhost:8090` (or `http://<SERVER_IP>:8090`).
- Activates the **33 MCP tools** for 100% external agent autopilot control.

---

### Step 4: Launch Full Business Suite & WhatsApp (`./tools/install_full_business_suite_linux.sh`)

This step provisions the complete enterprise CRM/ERP backend and WhatsApp gateway:

```bash
./tools/install_full_business_suite_linux.sh
```

**What this step does automatically:**
- Spawns Docker containers for:
  - **MariaDB** & **Redis** (Cache, Queue, SocketIO)
  - **Frappe Framework / ERPNext** backend & frontend (`:8080`)
  - **Frappe CRM**, **Helpdesk**, **Telephony**, and **PhoneAgent Frappe** apps
  - **OpenWA WhatsApp API** sidecar (`:2785`)
- Automatically migrates databases and creates the default site `phoneagent.localhost`.
- Generates admin credentials and provisions permanent API keys:
  - Writes Frappe API secret to `~/.config/phone-agent/frappe.json`.
  - Configures OpenWA session & API key in `~/.config/phone-agent/openwa.json`.
- Links all 14 CRM tools and 10 WhatsApp tools directly to the live phone call voice pipeline.

---

## 📱 Step 5: Pairing Handset & WhatsApp

### 1. Linking WhatsApp
1. Open **WhatsApp** on your phone.
2. Navigate to **Settings > Linked Devices > Link a Device**.
3. Point your camera at the QR code generated in PhoneAgent Studio (or view `whatsapp_qr.png` in the repo root).
4. Status will change to `ready` with your phone number active.

### 2. Linking Android Handset (Cellular GSM Voice Gateway)
1. Install `android_service_apk` on your rooted Android handset.
2. In **PhoneAgent Studio** (`http://<SERVER_IP>:8090`), click **Pair Handset**.
3. Open the PhoneAgent app on your phone and tap **Scan QR**.
4. Scan the pairing QR code from the Studio screen — your phone is now linked over the secure encrypted network tunnel (`:8770`).

---

## 🤖 Step 6: External AI Agent Integration (MCP Autopilot)

PhoneAgent includes a built-in **Model Context Protocol (MCP)** server with **33 autonomous tools** allowing external agents (Hermes, Codex, Claude Desktop, Cursor) to manage the entire appliance on 100% autopilot.

### Connecting an External Agent over SSH

On the external computer running your agent, add PhoneAgent to your MCP configuration (`mcp.json` or `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "phone-agent": {
      "command": "ssh",
      "args": [
        "-o", "StrictHostKeyChecking=no",
        "Ubuntu@<YOUR_SERVER_IP>",
        "docker exec -i phoneagent-core python3.11 -m phone_agent_gateway.ai_bridge.mcp_server"
      ]
    }
  }
}
```

### Autopilot Capabilities
- **Autopilot Outbound Dialing**: Calls start immediately without manual human approval modals.
- **Dynamic Model Switching**: External agents can switch between vLLM, Ollama, OpenAI, and Gemini at runtime.
- **Automated CRM Management**: Proactively creates leads, logs call summaries, and updates support tickets in Frappe CRM.
- **Autonomous WhatsApp**: Dispatches messages, PDF catalogs, locations, and images during or after phone calls.

---

## 🌐 Services & Ports Cheat Sheet

| Service | Port / URL | Credentials Location |
| :--- | :--- | :--- |
| **PhoneAgent Studio** | `http://<SERVER_IP>:8090` | Bearer token in `~/.config/phone-agent/control.token` |
| **Frappe CRM & ERPNext** | `http://<SERVER_IP>:8080` | Saved in `~/.config/phone-agent/frappe.json` |
| **OpenWA WhatsApp API** | `http://<SERVER_IP>:2785` | Saved in `~/.config/phone-agent/openwa.json` |
| **Handset Network Tunnel** | `tcp://<SERVER_IP>:8770` | Encrypted 32-byte key in `~/.config/phone-agent/link.key` |

---

## 🔍 Verification & Health Checks

Verify all systems with these one-line commands:

```bash
# Check Docker container status (all containers should be Up / Healthy)
docker ps

# Test OpenWA WhatsApp connectivity
curl -s -X POST http://localhost:8090/api/openwa/test

# Test Frappe CRM connectivity
curl -s -X POST http://localhost:8090/api/frappe/test

# Check Studio Gateway status
curl -s http://localhost:8090/api/status
```
