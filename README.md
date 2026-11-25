<div align="center">

# amv-gn-bridge

**Local OpenAI-compatible REST API (`:11501`), FastMCP stdio server, desktop GUI, and browser sidebar bridge for multi-account Google Gemini Web with automatic quota failover.**

[FOR_AI_README (Autonomous Agent Setup & Operating Manual)](FOR_AI_README.md) · [CHANGELOG](CHANGELOG.md) · [Releases](https://github.com/uno-km/amv-gn-bridge/releases)

<p>
  <img src="https://img.shields.io/badge/v1.0.1-blue?style=flat-square" alt="version">
  <img src="https://img.shields.io/badge/Windows%20%7C%20macOS%20%7C%20Linux-supported-green?style=flat-square" alt="platform">
  <img src="https://img.shields.io/badge/License-Apache--2.0-blue?style=flat-square" alt="license">
  <img src="https://img.shields.io/badge/Multi--Account-Auto%20Failover-indigo?style=flat-square" alt="failover">
  <img src="https://img.shields.io/badge/Quota%20Timer-HH%3AMM%3ASS%20Live-orange?style=flat-square" alt="countdown">
  <img src="https://img.shields.io/badge/FastMCP-Native-purple?style=flat-square" alt="fastmcp">
</p>

</div>

---

> **Want an AI coding agent to install and wire this up for you?**
> Paste `https://github.com/uno-km/amv-gn-bridge` into Claude Code, Cursor, Antigravity, Windsurf, or Codex and ask it to follow **[FOR_AI_README.md](FOR_AI_README.md)**. It includes a complete step-by-step installation, MCP registration, verification, and troubleshooting playbook.

---

## Overview

`amv-gn-bridge` connects one or more Google Gemini Web (`https://gemini.google.com/app`) accounts—each isolated in its own local browser profile and Chrome DevTools Protocol (CDP) port (`9337+`)—to a single local OpenAI-compatible endpoint (`http://127.0.0.1:11501`).

- **Multi-Account Pool & Automatic Quota Failover**: Detects daily image or text limits via Gemini's `"Create images" / "이미지 만들기"` `aria-disabled` state or quota notice text, parses the reset timestamp (falling back to a configurable cooldown), tears down the exhausted browser process tree, and automatically fails over to the next available account.
- **Live `HH:MM:SS` Per-Account Cooldown Countdown**: Tracks remaining and elapsed cooldown time per account in `~/.amv_gn_bridge/account_meta.json`, rendered live in both the desktop GUI and web dashboard (`/dashboard`), with automatic pool re-entry upon expiry.
- **Strict FIFO Queue & Parallel Dispatch**: Spreads simultaneous requests across distinct idle accounts immediately and queues excess bursts in strict FIFO order (`queued_count` / `active_count` reported on `/api/status`).
- **Low-Footprint Browser Lifecycle**: Enforces `max_active_browsers` (default `1`), prevents Chromium session-restore tab bloat (`restore_on_startup=5`, stale `Tabs_*`/`Session_*` pruning while preserving cookies), trims extra tabs to a single page, recycles renderer tabs every 15 requests in `hot` mode, and cleans up full process trees on teardown.
- **Unified Interface & Coexistence**: Exposes OpenAI-compatible `/v1/chat/completions` and `/v1/images/generations`, FastMCP stdio tools, a Chrome/Edge/Whale Side Panel extension (`Ctrl+Shift+G`), SQLite WAL telemetry (`~/.amv_gn_bridge/telemetry.db`), and a SHA-256 verified GitHub Release hot-swap auto-updater. Runs cleanly alongside [`amv-cp-bridge`](https://github.com/uno-km/amv-cp-bridge) (`:11500` / CDP `9333–9336`) without port or profile collisions.

---

## Prerequisites

- **Microsoft Edge** or **Google Chrome** installed locally (the bridge connects to your installed browser over CDP—do **not** run `playwright install`).
- One or more Google accounts with access to `https://gemini.google.com/app`.
- For source installs: **Python 3.10+** (not required when using the Windows single-file EXE).

---

## Quick Start

### Option 1: One-Line Installer (Recommended)

**Windows (PowerShell)** — downloads the latest verified `amv-gn-bridge.exe` + browser extension to `%LOCALAPPDATA%\amv-gn-bridge`, adds it to User `PATH`, and launches the GUI & `:11501` gateway:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/uno-km/amv-gn-bridge/main/scripts/setup_all.ps1 | iex"
```

*(For headless/scripted installs without launching the GUI, set `$env:AMV_NO_LAUNCH = "1"` first.)*

**macOS / Linux (Bash)** — installs into `~/.amv_gn_bridge/venv`, runs `selftest`, and links `~/.local/bin/amv-gn-bridge`:

```bash
curl -fsSL https://raw.githubusercontent.com/uno-km/amv-gn-bridge/main/scripts/setup_all.sh | bash
```

### Option 2: Direct Windows EXE Download

Download `amv-gn-bridge.exe` from [GitHub Releases](https://github.com/uno-km/amv-gn-bridge/releases/latest) and run it directly. No Python installation is required.

### Option 3: Install from Source or Release Wheel

```bash
git clone https://github.com/uno-km/amv-gn-bridge.git && cd amv-gn-bridge && pip install -e . && amv-gn-bridge dashboard
```

Or install the wheel directly from [Releases](https://github.com/uno-km/amv-gn-bridge/releases/latest):

```bash
pip install https://github.com/uno-km/amv-gn-bridge/releases/latest/download/amv_gn_bridge-1.0.1-py3-none-any.whl
```

---

## First-Run Account Sign-In

1. Open the desktop GUI or browse to `http://127.0.0.1:11501/dashboard`.
2. Add one or more Google accounts (`+ Add Account` / `POST /api/accounts`).
3. Click **Connect Account** (or call `POST /api/accounts/{port}/login`) to open an isolated browser window and sign in to Google Gemini once.
4. Session cookies remain local in that account's isolated profile directory; no credentials are transmitted to any third-party service.

---

## OpenAI-Compatible REST API (`http://127.0.0.1:11501`)

### Text & Multimodal Chat (`POST /v1/chat/completions`)

**macOS / Linux / Git Bash**
```bash
curl -s http://127.0.0.1:11501/v1/chat/completions -H "Content-Type: application/json" -d '{"model":"gemini-web","messages":[{"role":"user","content":"Summarize SQLite WAL mode in 3 bullet points."}]}'
```

**Windows PowerShell**
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:11501/v1/chat/completions" -Method Post -ContentType "application/json" -Body '{"model":"gemini-web","messages":[{"role":"user","content":"Summarize SQLite WAL mode in 3 bullet points."}]}' | ConvertTo-Json -Depth 6
```

Supported models: `gemini-web`, `gemini-web-thinking`, `gemini-web-image`. Supports both `stream: true` (SSE) and `stream: false`.

### Image Generation (`POST /v1/images/generations`)

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:11501/v1/images/generations" -Method Post -ContentType "application/json" -Body '{"prompt":"A photorealistic vertical 9:16 street scene in Seoul at dusk","model":"gemini-web-image"}'
```

Optional `"reference_images"` accepts base64 `data:image/...` URIs or local file paths for character/style consistency.

---

## CLI Reference

| Command | Description |
| :--- | :--- |
| `amv-gn-bridge` | Launch native desktop GUI + embedded `:11501` gateway |
| `amv-gn-bridge dashboard` | Start `:11501` gateway and open web dashboard in browser |
| `amv-gn-bridge gateway --port 11501` | Run headless REST API gateway only |
| `amv-gn-bridge mcp` (alias: `serve`) | Run FastMCP stdio server for IDE/agent clients |
| `amv-gn-bridge status` | Print account pool and live `HH:MM:SS` cooldown summary |
| `amv-gn-bridge ask "<prompt>"` | Send a one-shot query from the terminal |
| `amv-gn-bridge purge` | Terminate idle Gemini browser process trees |
| `amv-gn-bridge selftest` | Run built-in integrity check |

---

## FastMCP Integration (Claude Desktop, Cursor, Antigravity)

Add the following entry to your MCP configuration (`claude_desktop_config.json`, Cursor MCP settings, or Antigravity `mcp_config.json`):

```json
{
  "mcpServers": {
    "gemini-bridge": {
      "command": "amv-gn-bridge",
      "args": ["mcp"]
    }
  }
}
```

If using a specific Python environment instead of the EXE on `PATH`:

```json
{
  "mcpServers": {
    "gemini-bridge": {
      "command": "python",
      "args": ["-m", "amv_gn_bridge.cli", "mcp"]
    }
  }
}
```

Exposed tools:
- `ask_gemini(prompt, mode="auto")` (`"auto"` or `"thinking"`)
- `generate_gemini_image(prompt, output_path, reference_images=None)`
- `get_gemini_status()`

---

## Browser Sidebar Extension (Chrome / Edge / Whale)

1. Download and unzip [`amv-gn-bridge-extension.zip`](https://github.com/uno-km/amv-gn-bridge/releases/latest/download/amv-gn-bridge-extension.zip) (or use the `extension/` folder unpacked by `setup_all.ps1` / repository checkout).
2. Open `chrome://extensions`, `edge://extensions`, or `whale://extensions` and enable **Developer mode**.
3. Click **Load unpacked** and select the `extension/` folder.
4. Toggle the side panel with `Ctrl+Shift+G` (`Cmd+Shift+G` on macOS).

---

## Building the Windows Single-File EXE

```bash
python scripts/build/build_exe.py
```

Generates `dist/amv-gn-bridge.exe`, `dist/amv-gn-bridge.exe.sha256`, and `dist/build_report.json` after validating the binary via `selftest`. Running EXE instances poll GitHub Releases every 30 minutes and support SHA-256 verified hot-swap updates with `.bak` rollback safety.

---

## License

Copyright © 2026 Eunho Kim / AMEVA Foundation.  
SPDX-License-Identifier: `Apache-2.0 OR Commercial-License`
