# Changelog

## 1.0.1

### Added
- **CLI Parity (`purge` & `serve` alias)**: Added `amv-gn-bridge purge` command to terminate idle Gemini browser process trees and `serve` alias for `mcp` so CLI workflows align with `amv-cp-bridge`.
- **Cross-Platform One-Line Installers**: Enhanced `scripts/setup_all.ps1` (downloads and unpacks `amv-gn-bridge-extension.zip`, adds install directory to User `PATH`, supports `AMV_NO_LAUNCH=1` for headless/agent installs) and added `scripts/setup_all.sh` for macOS/Linux.

### Changed
- **English-First `README.md` & Upgraded `FOR_AI_README.md`**: Rewrote `README.md` in clear, concise English and expanded `FOR_AI_README.md` with a step-by-step autonomous zero-to-working installation, MCP registration, verification, and sign-in handoff playbook for AI coding agents.

## 1.0.0

### Changed & Optimized
- **Session Bloat Prevention & Single-Tab Enforcement**: `_sanitize_profile_exit_type()` now sets `session.restore_on_startup = 5` and `_prune_stale_session_files()` removes accumulated `Default/Sessions/Tabs_*` and `Session_*` files on launch while preserving authentication cookies and preferences. `connect()` trims extra browser tabs down to a single active page, and `verify_login()` reuses the cold-boot startup tab instead of spawning an extra tab.
- **Fast Startup Failure Cleanup & Non-Blocking Teardown**: `ensure_running()` detects immediate child-process exits (e.g., Chromium singleton handoff) via `proc.poll()` and invokes `kill_browser_process(force=True)` before raising `RuntimeError`; `cleanup()` avoids blocking CDP `page.close()` / `context.close()` calls.
- **Persistent Login Window (`open_login`)**: Opening a browser window for an already signed-in account now keeps the window open until closed by the user or timeout expiry (`hold_if_signed_in=True`) instead of auto-closing after 1 second.
- **Image Creation Menu Retry**: Added up to 3 retry attempts (`Escape` reset + `10000ms` click timeout) around `"업로드 및 도구" -> "이미지 만들기"` selection in `generate_image()` to absorb transient UI overlay delays while preserving immediate `aria-disabled` `GeminiQuotaError` detection.
- **Strict FIFO Queue & Accurate Queue/Active Metrics**: Added a FIFO ticket queue (`_wait_tickets`, `_ticket_seq`) to `GeminiPool._acquire_next_worker()` so excess concurrent requests wait in strict arrival order once all available accounts are busy, while preserving immediate parallel dispatch across idle accounts and immediate `GeminiAllQuotaExhausted` when all accounts are cooling down. Exposed `queued_count` / `queued_tasks` on `/api/status` and `/health`.
- **MCP Double-Launch Guard & Extended Timeout**: `mcp_server.py` now falls back to direct local `engine` execution only on `ConnectionRefusedError` (preventing Edge profile double-launch collisions on gateway `HTTPError` or `TimeoutError`) and raises the default gateway timeout to `360s` (`GEMINI_MCP_TIMEOUT_SEC`).
- **Async Gateway Status & Scalable GUI Verify Timeout**: Offloaded `engine.list_accounts()` via `asyncio.to_thread()` in `/health`, `/api/status`, and `/api/accounts`; scaled `_verify_all()` GUI request timeout dynamically with account count.

### Verified
- 15/15 unit tests passing (`pytest tests/ -v`), including session pruning/cookie preservation, extra-tab trimming, early-exit process cleanup, FIFO queue ordering with `active_count`/`queued_count` separation, and MCP double-launch guard.
- Live multi-account verification confirmed single-tab launch (`page_tabs=1`), non-blocking `/api/status`, live `/api/accounts/{port}/verify`, FastMCP `stdio` handshake, and zero residual Edge processes after `/api/purge`.

## 0.1.3

### Fixed
- Port-holder lookup (`netstat -ano` / `lsof`) decoded its output strictly; on a Windows console whose code page is not UTF-8 (Korean cp949) a decode error ended the reader thread and the port check silently found no process. Output is now decoded with `errors="replace"`.

### Verified
- 10 unit tests, `selftest` and a live gateway run from the installed wheel (health, status, models, accounts, settings; chat without a logged-in account ends with an explicit error, not a fake answer).

## 0.1.2

### Fixed
- **True Concurrent Multi-Account Dispatch (`_pending_count` + `_queue_lock`)**: Implemented `_acquire_next_worker()` under `GeminiPool._queue_lock` and replaced the unused `_reserved` boolean with an integer `_pending_count` counter, ensuring simultaneous parallel requests spread across distinct idle accounts rather than piling onto the same worker.
- **Fast Failover on Google `/sorry` Redirects**: Added `_is_sorry_url()` detection in `_prepare_page()` and `_wait_for_signed_in_composer(..., fail_fast_sorry=True)` so Google traffic-verification redirects immediately place the affected account in cooldown and fail over instead of polling for 45 seconds.

## 0.1.1

### Fixed
- **Fast `/health` & `list_accounts()` Port Probing**: Added a `0.08s` localhost TCP socket pre-check with `ProxyHandler({})` before `/json/version` in `GeminiWorker.is_alive()` and parallelized `GeminiPool.list_accounts()` with `ThreadPoolExecutor`, reducing multi-account status checks from `3.1s+` to `<0.15s` and preventing `/health` client timeouts.
- **GitHub Actions PyInstaller Release Workflow**: Removed unused `collect_all("mcp")` CLI import dependency in `amv-gn-bridge.spec` and gated GitHub Release publishing on `refs/tags/v*` in `.github/workflows/release.yml`.

## 0.1.0

### Added
- **Multi-Account Google Gemini Web Pool (`GeminiPool` & `GeminiWorker`)**: Unlimited Google account registration with isolated CDP ports (`9337+`) and dedicated browser profiles.
- **Automatic Quota Detection & Next-Account Failover**: Detects daily image/text limits via `"이미지 만들기" / "Create images"` menu `aria-disabled` state and response text, parses exact reset times (`"오후 X:XX에 재설정"` / `"Resets at X:XX PM"`), closes the exhausted account's browser tree, and automatically fails over to the next available account.
- **Real-Time Per-Account `HH:MM:SS` Cooldown Countdown**: Tracks `quota_hit_at`, `cooldown_until`, `cooldown_remaining_hms`, and `quota_elapsed_hms` in `~/.amv_gn_bridge/account_meta.json`, displayed live every second in both the native CustomTkinter GUI and the web dashboard (`/dashboard`) with automatic recovery on expiry.
- **16GB RAM Protection & Hot-Mode Tab Recycling**: Enforces `max_active_browsers=1`, recycles renderer tabs every 15 requests (`_recycle_tab`), suppresses crash-restore bubbles (`_sanitize_profile_exit_type` + `--disable-background-mode`), and terminates full process trees (`taskkill /F /T`).
- **OpenAI-Compatible REST Gateway (`:11501`), SQLite Telemetry DB, FastMCP Server & Browser Sidebar Extension**:
  - `POST /v1/chat/completions` (streaming SSE & non-streaming JSON)
  - `POST /v1/images/generations` (reference image attachment, high-res download extraction, SHA-256/dimension reference copy filtering)
  - Embedded SQLite WAL telemetry (`~/.amv_gn_bridge/telemetry.db`) with per-account statistics
  - FastMCP tools: `ask_gemini`, `generate_gemini_image`, `get_gemini_status`
  - Chrome/Edge/Whale Manifest V3 Side Panel extension (`Ctrl+Shift+G`)
- **Single-File Windows EXE Build & Dynamic GitHub Release Hot-Swap Auto-Updater**:
  - PyInstaller single-file executable (`dist/amv-gn-bridge.exe`) with boot splash and automated `selftest`
  - Dynamic 30-minute GitHub Release polling (`/api/update/check`) and atomic PowerShell hot-swap auto-restart (`/api/update/apply`) with SHA-256 verification and `.bak` rollback safety
  - GitHub Actions automated release workflow (`.github/workflows/release.yml`) and one-click installer (`scripts/setup_all.ps1`)
