# FOR_AI_README.md — Autonomous Agent Setup & Operating Manual (`amv-gn-bridge`)

> **To Any Autonomous AI Agent (Claude Code, Cursor, Antigravity, Codex, Devin, Windsurf, Aider, OpenCode, or any LLM orchestrator):**
> If a developer hands you `https://github.com/uno-km/amv-gn-bridge` and asks you to install, configure, integrate, or operate `amv-gn-bridge`, follow this manual sequentially. Every command, port, API payload, and invariant below is verified against the current codebase.

---

## 0. Ground Truth Summary

`amv-gn-bridge` is a local OpenAI-compatible REST gateway (`http://127.0.0.1:11501`), FastMCP stdio server, native desktop GUI, and Chrome/Edge/Whale Side Panel extension that orchestrates **a multi-account pool of Google Gemini Web (`https://gemini.google.com/app`) accounts** via isolated Chrome DevTools Protocol (CDP) browser profiles (`9337+`).

### Key Operational Invariants
1. **Uses Installed Edge/Chrome — Do NOT Download Playwright Browsers**: `amv-gn-bridge` launches the system-installed Microsoft Edge or Google Chrome via `--remote-debugging-port` and connects via `playwright.chromium.connect_over_cdp`. **Never run `playwright install`**; it downloads an unnecessary 300MB+ browser binary.
2. **Coexistence with `amv-cp-bridge`**:
   - `amv-cp-bridge` (M365 Copilot): Gateway `:11500`, CDP ports `9333–9336`, state `~/.amv_cp_bridge/`.
   - `amv-gn-bridge` (Google Gemini): Gateway `:11501`, CDP ports `9337+`, state `~/.amv_gn_bridge/`.
   - Both bridges are designed to run simultaneously without port or profile conflicts.
3. **Multi-Account Failover & Live `HH:MM:SS` Cooldowns**:
   - Quota limits are detected via Gemini's `"Create images" / "이미지 만들기"` `aria-disabled="true"` state or quota response text.
   - Exhausted accounts record `quota_hit_at` and `cooldown_until` in `~/.amv_gn_bridge/account_meta.json`, terminate their browser process tree, and automatically fail over to the next available account.
4. **Strict FIFO Queue & Parallel Dispatch**:
   - Simultaneous requests spread immediately across distinct idle accounts.
   - Requests exceeding available non-cooling-down accounts wait in strict FIFO ticket order (`_wait_tickets`), accurately tracked via `active_count` / `queued_count` on `/api/status` and `active_tasks` / `queued_tasks` on `/health`.
   - When all registered accounts are cooling down, requests fail immediately with HTTP `429` (`quota_exhausted`) and the earliest recovery timestamp (`HH:MM:SS`).
5. **Session Bloat Prevention & Single-Tab Enforcement**:
   - Before browser launch, `_sanitize_profile_exit_type()` writes `exit_type="Normal"` and `session.restore_on_startup=5`, and `_prune_stale_session_files()` removes accumulated `Default/Sessions/Tabs_*` and `Session_*` files while preserving cookies and preferences.
   - `connect()` trims extra pages to 1 tab; `verify_login()` reuses the cold-boot startup tab; `hot` mode recycles renderer tabs every `GEMINI_TAB_RECYCLE_INTERVAL` (default `15`) requests.
6. **MCP Double-Launch Guard**:
   - `mcp_server.py` routes through a running `:11501` gateway with `GEMINI_MCP_TIMEOUT_SEC` (default `360s`).
   - It falls back to direct local `engine` execution **only** on `ConnectionRefusedError` (when no gateway is running). On HTTP errors (`401`/`422`/`429`/`500`) or timeouts, it returns an explicit error instead of spawning a colliding local browser on the same profile.

---

## 1. Zero-to-Working Autonomous Installation Playbook

Execute these steps in order when asked to install and set up `amv-gn-bridge` on a fresh machine.

### Step 1 — Pre-Flight Check

Check whether the gateway is already running and whether Edge or Chrome is installed:

**Windows (PowerShell):**
```powershell
try { Invoke-RestMethod "http://127.0.0.1:11501/health" -TimeoutSec 2 } catch { "NOT_RUNNING" }
Test-Path "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
Test-Path "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

**macOS / Linux (Bash):**
```bash
curl -sf --max-time 2 http://127.0.0.1:11501/health || echo "NOT_RUNNING"
which google-chrome chromium microsoft-edge 2>/dev/null || ls "/Applications/Microsoft Edge.app" "/Applications/Google Chrome.app" 2>/dev/null
```

If `/health` returns `{"ok": true, "gateway": "amv-gn-bridge", ...}` and `accounts_available >= 1`, skip directly to Step 5 (Integration).

### Step 2 — Install `amv-gn-bridge`

Choose the method matching the host OS:

- **Option A — Windows Single-File EXE (fastest, no Python needed):**
  ```powershell
  $env:AMV_NO_LAUNCH = "1"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/uno-km/amv-gn-bridge/main/scripts/setup_all.ps1 | iex"
  & "$env:LOCALAPPDATA\amv-gn-bridge\amv-gn-bridge.exe" selftest
  ```
  *(Installs `%LOCALAPPDATA%\amv-gn-bridge\amv-gn-bridge.exe`, unpacks the browser extension to `%LOCALAPPDATA%\amv-gn-bridge\extension`, and adds the directory to User `PATH`.)*

- **Option B — Windows / macOS / Linux Python Editable Install (for developers or MCP python invocation):**
  ```bash
  git clone https://github.com/uno-km/amv-gn-bridge.git
  cd amv-gn-bridge
  python -m pip install -e .
  python -m amv_gn_bridge.cli selftest
  ```

- **Option C — macOS / Linux One-Liner:**
  ```bash
  AMV_NO_LAUNCH=1 curl -fsSL https://raw.githubusercontent.com/uno-km/amv-gn-bridge/main/scripts/setup_all.sh | bash
  ~/.amv_gn_bridge/start.sh selftest
  ```

Verify `selftest` outputs `"ok": true` before continuing.

### Step 3 — Start the `:11501` Gateway

- **Headless gateway only (recommended for automation/agents):**
  - Windows EXE: `Start-Process "$env:LOCALAPPDATA\amv-gn-bridge\amv-gn-bridge.exe" -ArgumentList "gateway","--port","11501" -WindowStyle Hidden`
  - Python (Windows): `Start-Process "python" -ArgumentList "-m","amv_gn_bridge.cli","gateway","--port","11501" -WindowStyle Hidden`
  - macOS / Linux: `nohup amv-gn-bridge gateway --port 11501 >/dev/null 2>&1 &`
- **Desktop GUI + gateway:** launch `amv-gn-bridge` (or `amv-gn-bridge.exe`) with no arguments.

Wait 2–4 seconds and verify:
```bash
curl -sf http://127.0.0.1:11501/health
```
Expected fields: `ok: true`, `gateway: "amv-gn-bridge"`, `accounts_total`, `accounts_available`, `active_tasks`, `queued_tasks`.

### Step 4 — Account Registration & Human-in-the-Loop Google Sign-In

1. Inspect existing accounts:
   ```bash
   curl -sf http://127.0.0.1:11501/api/accounts
   ```
2. If you need to register additional Google accounts (e.g., for higher daily image quota):
   ```bash
   curl -sf -X POST http://127.0.0.1:11501/api/accounts \
     -H "Content-Type: application/json" \
     -d '{"name":"acct2","browser":"edge"}'
   ```
3. **Human sign-in handoff (the only step requiring human interaction)**:
   - Call `POST http://127.0.0.1:11501/api/accounts/{port}/login` (or ask the user to click **Connect Account** in `http://127.0.0.1:11501/dashboard`).
   - A visible browser window opens to `https://gemini.google.com/app`. Ask the user to sign in to their Google account.
   - Once signed in, verify programmatically:
     ```bash
     curl -sf -X POST http://127.0.0.1:11501/api/accounts/9337/verify
     ```
     Confirm `login_state == "ok"` (or `"quota"` if that account's daily image limit is currently active).

### Step 5 — Wire Up MCP (Optional — When Asked to Connect to IDE/Agent)

Register in the target client's MCP JSON (`mcp_config.json`, `claude_desktop_config.json`, or Cursor MCP settings):

- Using installed Windows EXE:
  ```json
  {
    "mcpServers": {
      "gemini-bridge": {
        "command": "C:\\Users\\<USER>\\AppData\\Local\\amv-gn-bridge\\amv-gn-bridge.exe",
        "args": ["mcp"]
      }
    }
  }
  ```
- Using Python module directly (prefer absolute Python path to avoid venv/PATH drift):
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

Verify MCP connectivity by sending an MCP `tools/call` for `get_gemini_status` or invoking `ask_gemini` / `generate_gemini_image`.

---

## 2. System Architecture & Persistent Files

```text
[AI Agent / OpenAI SDK Client / Browser Side Panel Extension]
         │
         ├── REST API:  POST http://127.0.0.1:11501/v1/chat/completions
         │              POST http://127.0.0.1:11501/v1/images/generations
         ├── FastMCP:   ask_gemini / generate_gemini_image / get_gemini_status
         └── Extension: Chrome / Edge / Whale Side Panel (Ctrl+Shift+G)
         │
         ▼
[FastAPI Gateway — Port 11501] (amv_gn_bridge/gateway.py)
         │  • Async status endpoints (asyncio.to_thread for account probing)
         ▼
[GeminiPool — Parallel Dispatch, FIFO Queue, Failover & RAM Guard] (amv_gn_bridge/engine.py)
         │  • Immediate dispatch to idle non-cooling-down workers
         │  • Strict FIFO ticket queue (_wait_tickets) when all workers are busy
         │  • Enforces max_active_browsers (default 1)
         │  • Records telemetry to ~/.amv_gn_bridge/telemetry.db (SQLite WAL)
         ▼
[GeminiWorker — CDP Port 9337, 9338, 9339, ...] (amv_gn_bridge/engine.py)
         │  • Dedicated profile dir per account
         │  • restore_on_startup=5 + stale Tabs_*/Session_* pruning (cookies preserved)
         │  • Single-tab enforcement + 15-request hot-mode tab recycling
         │  • Full process tree teardown on quota, cold mode, or standby expiry
```

| File | Purpose |
| :--- | :--- |
| `~/.amv_gn_bridge/accounts.json` | Registered account pool (`port`, `name`, `profile_dir`, `browser`, `email`) |
| `~/.amv_gn_bridge/account_state.json` | Per-port login state (`"ok"`, `"quota"`, `"required"`, `"unknown"`) |
| `~/.amv_gn_bridge/account_meta.json` | Per-port identity, verification time, and quota cooldown timestamps |
| `~/.amv_gn_bridge/settings.json` | Runtime config (`mode`, `standby_timeout_sec`, `quota_cooldown_sec`, `login_cooldown_sec`, `min_gap_sec`, `max_active_browsers`) |
| `~/.amv_gn_bridge/telemetry.db` | SQLite WAL query log and per-account usage summary |

---

## 3. OpenAI-Compatible REST API Contract (`http://127.0.0.1:11501`)

### 3.1 Text & Multimodal Chat (`POST /v1/chat/completions`)

- **Models**: `"gemini-web"` (default), `"gemini-web-thinking"` (adds step-by-step analysis directive)
- **Streaming**: `stream: true` (SSE `chat.completion.chunk` + `data: [DONE]`) or `stream: false` (JSON `chat.completion`)
- **Attachments**: Pass base64 data URLs (`data:image/png;base64,...`) or local paths via `reference_images` or OpenAI `image_url` blocks.

### 3.2 Native Image Generation (`POST /v1/images/generations`)

```json
{
  "prompt": "Vertical 9:16 candid documentary smartphone photo of a cozy studio desk at night",
  "model": "gemini-web-image",
  "reference_images": [
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg..."
  ],
  "timeout_sec": 240.0,
  "min_bytes": 50000,
  "reset_chat": true
}
```

Response (`200 OK`):
```json
{
  "created": 1763515200,
  "worker_port": 9337,
  "account_name": "main",
  "text": "",
  "data": [
    {
      "b64_json": "iVBORw0KGgoAAAANSUhEUg...",
      "mime_type": "image/png",
      "sha256": "...",
      "bytes_len": 1482910
    }
  ]
}
```

### 3.3 Structured Error Codes

| HTTP Status | `error.type` | Meaning & Required Agent Action |
| :--- | :--- | :--- |
| `401` | `login_required` | Account session expired or redirected to Google sign-in / `/sorry`. Call `POST /api/accounts/{port}/login` and prompt the user to authenticate. |
| `422` | `policy_refusal` | Gemini safety policy refused the prompt. **Do not retry unchanged on other accounts**; adjust the prompt. |
| `429` | `quota_exhausted` | **All** registered accounts are in cooldown. Inspect the error message for the earliest recovery account and `HH:MM:SS` countdown, or ask the user to add another account. |
| `500` | `RuntimeError` / `TimeoutError` | Transient browser or network failure across all tried candidates. Run `POST /api/purge` and retry once. |

---

## 4. Management & Diagnostics Endpoints

| Method | Path | Purpose |
| :--- | :--- | :--- |
| `GET` | `/health` | Health check (`ok`, `gateway`, `version`, `accounts_total`, `accounts_available`, `active_tasks`, `queued_tasks`) |
| `GET` | `/api/status` | Full dashboard state (`active_count`, `queued_count`, workers with live `HH:MM:SS` cooldown fields, settings, telemetry summary) |
| `GET` | `/api/accounts` | Account list with live cooldown state |
| `POST` | `/api/accounts` | Add a Google account (`name`, optional `port`, `profile_dir`, `browser`, `email`) |
| `DELETE` | `/api/accounts/{port}` | Remove account from pool (disk profile is kept) |
| `POST` | `/api/accounts/{port}/login` | Launch visible browser window for sign-in (stays open until user closes it if already signed in) |
| `POST` | `/api/accounts/{port}/verify` | Live headless verification of login status and `"Create images"` quota availability |
| `POST` | `/api/accounts/verify` | Sequential verification of all accounts (1 browser at a time) |
| `POST` | `/api/accounts/{port}/reset-cooldown` | Manually clear an account's cooldown |
| `POST` | `/api/accounts/{port}/close` | Terminate browser process tree for one account |
| `POST` | `/api/purge` | Terminate all idle browser process trees |
| `GET` / `POST` | `/api/settings` | Inspect or update `mode` (`"hot"`/`"cold"`), `max_active_browsers`, `quota_cooldown_sec`, `min_gap_sec`, `standby_timeout_sec` |
| `GET` / `POST` | `/api/update/check`, `/api/update/apply` | Check GitHub Releases and apply SHA-256 verified EXE hot-swap update |

---

## 5. FastMCP Tools & Environment Variables

| Tool | Signature | Behavior |
| :--- | :--- | :--- |
| `ask_gemini` | `(prompt: str, mode: str = "auto") -> str` | Sends query (`"auto"` or `"thinking"`) via `:11501` gateway if alive, else local engine |
| `generate_gemini_image` | `(prompt: str, output_path: str, reference_images: list[str] \| None = None) -> str` | Generates image via multi-account pool and writes binary to `output_path` |
| `get_gemini_status` | `() -> str` | Returns concise status + remaining `HH:MM:SS` cooldown timers for all accounts |

| Env Var | Default | Purpose |
| :--- | :--- | :--- |
| `GEMINI_MCP_TIMEOUT_SEC` | `360` | HTTP timeout (seconds) when MCP calls `:11501` gateway |
| `GEMINI_TAB_RECYCLE_INTERVAL` | `15` | Requests per worker before recycling the Gemini renderer tab in `hot` mode |
| `AMV_NO_LAUNCH` | `0` | Set to `1` in `setup_all.ps1` / `setup_all.sh` to skip launching GUI after install |

---

## 6. Mandatory Rules for AI Agents

1. **Never spawn `msedge.exe` or `chrome.exe` manually against `~/.amv_gn_bridge` profiles.** Always route through `http://127.0.0.1:11501` or the MCP tools so the FIFO queue, RAM guard (`max_active_browsers`), and process tree teardown stay authoritative.
2. **Never commit personal emails, profile directories, cookies, or tokens** into any repository. All runtime state lives under `~/.amv_gn_bridge/`.
3. **Never run `playwright install`.** System Edge or Chrome is used directly over CDP.
4. **Do not retry HTTP `422` (`policy_refusal`) blindly.** Rewrite the prompt instead. On HTTP `429` (`quota_exhausted`), surface the remaining `HH:MM:SS` timer to the user.

---

*Copyright © 2026 Eunho Kim / AMEVA Foundation. SPDX-License-Identifier: Apache-2.0 OR Commercial-License.*
