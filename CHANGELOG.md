# Changelog

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
