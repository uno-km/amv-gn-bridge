# FOR_AI_README.md — Autonomous Agent Operating Manual (`amv-gn-bridge`)

> **To Any Autonomous AI Agent (Claude Code, Cursor, Antigravity, Devin, Windsurf, Aider, OpenCode, or any LLM-based orchestrator):**
> Read this manual before executing any command involving `amv-gn-bridge`. It documents the architecture, exact REST/MCP contracts, multi-account quota failover rules, real-time `HH:MM:SS` countdown semantics, memory management invariants, and dynamic GitHub Release hot-swap update protocol.

---

## 0. What This System Is (Ground Truth)

`amv-gn-bridge` is a local OpenAI-compatible REST gateway (`127.0.0.1:11501`), FastMCP stdio server, native desktop GUI, and browser sidebar extension that orchestrates **an unlimited pool of Google Gemini Web (`https://gemini.google.com/app`) accounts** via isolated Chrome DevTools Protocol (CDP) browser profiles (`9337+`).

### Core Capabilities
1. **Multi-Account Pool & Automatic Quota Failover**:
   - Operators can register and sign in to any number of Google accounts (`9337`, `9338`, `9339`, ...).
   - When an account hits its daily Gemini image or text quota (detected either via the `"이미지 만들기" / "Create images"` menu `aria-disabled="true"` attribute or response quota text), `amv-gn-bridge` immediately records the quota timestamp, parses the exact reset time (e.g., `"오후 5:23에 재설정"` / `"Resets at 5:23 PM"`), terminates that account's browser process tree (`taskkill /F /T`), and **automatically fails over to the next available account**.
2. **Real-Time `HH:MM:SS` Per-Account Cooldown Countdown**:
   - Every quota-limited account tracks `quota_hit_at`, `cooldown_until`, `cooldown_remaining_sec`, `cooldown_remaining_hms` (`"05:42:19"`), and `quota_elapsed_hms` (`"00:17:41"`), persisted to `~/.amv_gn_bridge/account_meta.json`.
   - When the countdown reaches `00:00:00`, the account automatically returns to the active pool (`login_state = "ok"`).
3. **16GB RAM Protection & Renderer Tab Recycling**:
   - `max_active_browsers` (default `1`) ensures that when switching to a new account, idle browser instances from other accounts are terminated first.
   - In `hot` mode (warm standby), each worker recycles its Gemini tab every `GEMINI_TAB_RECYCLE_INTERVAL` (default `15`) requests or after any timeout/DOM exception by opening a fresh tab before closing old tabs (`_recycle_tab`).
4. **Coexistence with `amv-cp-bridge`**:
   - `amv-cp-bridge` (M365 Copilot) runs on gateway port `11500` and CDP ports `9333–9336`.
   - `amv-gn-bridge` (Google Gemini) runs on gateway port `11501` and CDP ports `9337+`.
   - Both bridges can run simultaneously on the same machine without port or profile conflicts.

---

## 1. System Architecture & Data Layout

```text
[AI Agent / OpenAI SDK Client / Browser Sidebar Extension]
         │
         ├── (A) REST API:  POST http://127.0.0.1:11501/v1/chat/completions
         │                  POST http://127.0.0.1:11501/v1/images/generations
         ├── (B) MCP Tools: ask_gemini / generate_gemini_image / get_gemini_status
         └── (C) Extension: Chrome/Edge/Whale Side Panel (Ctrl+Shift+G)
         │
         ▼
[FastAPI Gateway — Port 11501] (amv_gn_bridge/gateway.py)
         │
         ▼
[GeminiPool — Multi-Account Failover & RAM Guard] (amv_gn_bridge/engine.py)
         │  • Filters out accounts where cooldown_until > now
         │  • Prioritizes warm, signed-in accounts, then round-robin
         │  • Enforces max_active_browsers (default 1) before launching next worker
         │  • Records every attempt to ~/.amv_gn_bridge/telemetry.db (SQLite WAL)
         │
         ▼
[GeminiWorker — CDP Port 9337, 9338, 9339, ...] (amv_gn_bridge/engine.py)
         │  • Dedicated profile dir per account
         │  • Crash-restore bubble suppression + Preferences exit_type="Normal"
         │  • Hot-mode tab recycling every 15 requests (_recycle_tab)
         │  • Full process tree teardown (/F /T) on quota, cold mode, or standby expiry
```

### Persistent Files (`~/.amv_gn_bridge/`)

| Path | Purpose |
| :--- | :--- |
| `~/.amv_gn_bridge/accounts.json` | Registered account list (`port`, `name`, `profile_dir`, `browser`, `email`) |
| `~/.amv_gn_bridge/account_state.json` | Per-port state (`"ok"`, `"quota"`, `"required"`, `"unknown"`) |
| `~/.amv_gn_bridge/account_meta.json` | Per-port metadata (`identity`, `detail`, `verified_at`, `quota_hit_at`, `cooldown_until`, `cooldown_kind`, `cooldown_reason`) |
| `~/.amv_gn_bridge/settings.json` | Runtime config (`mode`, `standby_timeout_sec`, `quota_cooldown_sec`, `login_cooldown_sec`, `min_gap_sec`, `max_active_browsers`) |
| `~/.amv_gn_bridge/telemetry.db` | SQLite WAL usage database (`usage_records` table — identical schema to `amv-cp-bridge` plus `by_account` aggregation) |

---

## 2. Pre-Flight Diagnostics for Agents

Run these checks before sending production workloads:

```powershell
# 1. Check if the gateway is alive on 11501
Invoke-RestMethod -Uri "http://127.0.0.1:11501/health" -TimeoutSec 3

# 2. Inspect all accounts and their live HH:MM:SS quota countdowns
Invoke-RestMethod -Uri "http://127.0.0.1:11501/api/accounts" -TimeoutSec 3 | ConvertTo-Json -Depth 5

# 3. If the gateway is not running, start it in the background
Start-Process -FilePath "python" -ArgumentList "-m","amv_gn_bridge.cli","gateway","--port","11501" -WindowStyle Hidden
```

---

## 3. OpenAI-Compatible REST API Reference (`http://127.0.0.1:11501`)

### 3.1 Text & Multimodal Chat (`POST /v1/chat/completions`)

- **Models**: `"gemini-web"` (default), `"gemini-web-thinking"` (step-by-step deep reasoning prefix)
- **Supports**: `stream: true` (SSE `chat.completion.chunk` + `data: [DONE]`) and `stream: false` (standard JSON `chat.completion`).
- **Multimodal attachments**: Pass base64 data URLs (`data:image/png;base64,...`) or local file paths in `reference_images` or OpenAI `image_url` content blocks.

```powershell
$body = @{
    model = "gemini-web"
    messages = @(@{ role = "user"; content = "Summarize the key differences between WAL and rollback journal in SQLite." })
    stream = $false
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Uri "http://127.0.0.1:11501/v1/chat/completions" -Method Post -ContentType "application/json" -Body $body
```

### 3.2 Native Image Generation (`POST /v1/images/generations`)

Generates an image via Gemini Web's image creation tool, downloads the original high-resolution bytes (falling back to DOM fetch if needed), filters out reference image copies by SHA-256 and dimensions, and returns base64-encoded PNG/JPEG data.

**Request Schema**:
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

**Success Response (`200 OK`)**:
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
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "bytes_len": 1482910
    }
  ]
}
```

### 3.3 Structured Error Codes

| HTTP Status | `error.type` | Meaning & Agent Action |
| :--- | :--- | :--- |
| `401` | `login_required` | Account session expired. Call `POST /api/accounts/{port}/login` or ask operator to sign in via GUI. |
| `422` | `policy_refusal` | Gemini safety policy refused the prompt. **Do not retry the same prompt on other accounts**; modify the prompt. |
| `429` | `quota_exhausted` | **All** registered accounts are currently in quota cooldown. The error message includes the earliest recovery account name, remaining `HH:MM:SS`, and exact recovery wall-clock time. |
| `500` | `RuntimeError` / `TimeoutError` | Transient browser or network failure across all candidate workers. |

---

## 4. Account Pool, Quota Countdown & Dynamic Update APIs

| Method | Path | Description |
| :--- | :--- | :--- |
| `GET` | `/health` | Health check (`gateway: "amv-gn-bridge"`, `accounts_total`, `accounts_available`, `active_tasks`) |
| `GET` | `/api/status` | Full dashboard state: workers with `cooldown_remaining_hms`, `quota_elapsed_hms`, `today_queries`, settings, and `telemetry.db` summary |
| `GET` | `/api/accounts` | List all accounts with real-time `HH:MM:SS` quota countdown fields |
| `POST` | `/api/accounts` | Add a new Google account (`{"name": "acct6", "email": "...", "port": 9342, "browser": "edge"}`) |
| `DELETE` | `/api/accounts/{port}` | Remove account from pool (profile folder on disk is preserved) |
| `POST` | `/api/accounts/{port}/login` | Launch visible browser window (`1100x860`) for manual Google sign-in |
| `POST` | `/api/accounts/{port}/verify` | Live headless check of Google login status AND `"이미지 만들기"` menu quota state |
| `POST` | `/api/accounts/verify` | Sequentially verify all accounts (1 browser at a time to protect 16GB RAM) |
| `POST` | `/api/accounts/{port}/reset-cooldown` | Manually clear quota/login cooldown for an account immediately |
| `POST` | `/api/accounts/{port}/close` | Terminate the browser process tree for a specific account |
| `POST` | `/api/purge` | Terminate all idle browser process trees across all accounts |
| `GET` | `/api/settings` | Read current engine settings |
| `POST` | `/api/settings` | Update `mode` (`"hot"` / `"cold"`), `quota_cooldown_sec`, `max_active_browsers`, `min_gap_sec`, `standby_timeout_sec` |
| `GET` | `/api/update/check` | Check GitHub Releases (`uno-km/amv-gn-bridge`) for a newer version |
| `POST` | `/api/update/apply` | Download latest `amv-gn-bridge.exe`, verify `.sha256`, hot-swap executable, and auto-restart |

---

## 5. FastMCP Integration (`amv_gn_bridge/mcp_server.py`)

### Tools Exposed
- `ask_gemini(prompt: str, mode: str = "auto") -> str`
  - `mode`: `"auto"` or `"thinking"`. Routes through `:11501` gateway if running, otherwise uses local `GeminiEngine`.
- `generate_gemini_image(prompt: str, output_path: str, reference_images: list[str] | None = None) -> str`
  - Generates an image across the multi-account pool and saves the binary directly to `output_path`.
- `get_gemini_status() -> str`
  - Returns one-line status summary of all accounts including remaining `HH:MM:SS` quota timers.

### MCP Config Registration (`mcp_config.json` / `claude_desktop_config.json`)
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
*(Or point `"command"` directly to `C:\\path\\to\\amv-gn-bridge.exe` with `"args": ["mcp"]`.)*

---

## 6. Mandatory Rules for AI Agents (Do / Don't)

1. **DO NOT spawn `msedge.exe` directly from client applications.** Always send HTTP requests to `http://127.0.0.1:11501/v1/images/generations` or `/v1/chat/completions`, or use the MCP tools. Direct browser spawning bypasses the RAM guard (`max_active_browsers=1`) and `/F /T` process tree cleanup.
2. **DO NOT hardcode personal account emails, profile paths, or cookies in source repositories.** All account configurations reside strictly in `~/.amv_gn_bridge/accounts.json` outside the Git workspace.
3. **Respect HTTP 422 (`policy_refusal`) vs HTTP 429 (`quota_exhausted`).** On `422`, rewrite or skip the prompt. On `429`, inspect the returned `HH:MM:SS` countdown or prompt the operator to add/login another Google account via `+ 계정 추가`.

---

*Copyright © 2026 Eunho Kim / AMEVA Foundation. Apache-2.0 OR Commercial-License.*
