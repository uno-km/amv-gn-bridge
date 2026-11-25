#!/usr/bin/env bash
# Copyright (c) 2026 Eunho Kim / AMEVA Foundation. All rights reserved.
# SPDX-License-Identifier: Apache-2.0 OR Commercial-License
# One-line installer & updater for amv-gn-bridge (macOS / Linux).
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/uno-km/amv-gn-bridge/main/scripts/setup_all.sh | bash
# Optional env vars:
#   AMV_GN_REPO   - GitHub repo (default: uno-km/amv-gn-bridge)
#   AMV_NO_LAUNCH - Set to "1" to skip launching after install

set -euo pipefail

ok()   { printf '\033[32m[ok]\033[0m %s\n' "$1"; }
info() { printf '[..] %s\n' "$1"; }
fail() { printf '\033[31m[err]\033[0m %s\n' "$1" >&2; exit 1; }

find_python() {
  local probe='import sys,venv; assert sys.version_info >= (3, 10); print(sys.executable)'
  local candidate output
  for candidate in \
    python3.12 python3.11 python3.10 python3 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3; do
    command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ] || continue
    if output=$("$candidate" -c "$probe" 2>/dev/null) && [ -n "$output" ]; then
      printf '%s' "$output"
      return 0
    fi
  done
  return 1
}

main() {
  local repo="${AMV_GN_REPO:-uno-km/amv-gn-bridge}"
  local root="$HOME/.amv_gn_bridge"
  local app="$root/app"
  local venv="$root/venv"
  local log="$root/setup.log"

  mkdir -p "$root" "$HOME/.local/bin"
  : >"$log"

  info "Checking latest GitHub Release from $repo..."
  local release tag
  release=$(curl -fsSL -H "Accept: application/vnd.github+json" -H "User-Agent: amv-gn-bridge-setup/1.0" \
    "https://api.github.com/repos/$repo/releases/latest") \
    || fail "Could not query GitHub Releases for $repo"
  tag=$(printf '%s' "$release" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
  [ -n "$tag" ] || fail "Latest release has no tag_name"
  ok "Latest release: $tag"

  local python
  python=$(find_python) || fail "Python 3.10+ with venv is required. Install Python 3.12 and re-run."
  ok "Python: $("$python" --version) ($python)"

  info "Downloading source archive for $tag..."
  local tmp="$root/source.tmp"
  rm -rf "$tmp"
  mkdir -p "$tmp"
  curl -fsSL "https://github.com/$repo/archive/refs/tags/$tag.tar.gz" | tar -xz -C "$tmp" --strip-components=1 \
    || fail "Failed to download source archive $tag"
  rm -rf "$app"
  mv "$tmp" "$app"
  ok "Source installed to $app"
  ok "Browser extension folder: $app/extension"

  if [ ! -x "$venv/bin/python" ]; then
    info "Creating virtual environment at $venv..."
    "$python" -m venv "$venv" >>"$log" 2>&1 || fail "Failed to create venv (see $log)"
  fi

  info "Installing Python dependencies (uses your installed Edge/Chrome; no Playwright Chromium download)..."
  "$venv/bin/python" -m pip install --upgrade --quiet --disable-pip-version-check pip setuptools wheel >>"$log" 2>&1 \
    || fail "pip upgrade failed (see $log)"
  "$venv/bin/python" -m pip install --upgrade --quiet --disable-pip-version-check -r "$app/requirements.txt" >>"$log" 2>&1 \
    || fail "Dependency install failed (see $log)"
  "$venv/bin/python" -m pip install --upgrade --force-reinstall --no-deps --quiet --disable-pip-version-check "$app" >>"$log" 2>&1 \
    || fail "Package install failed (see $log)"

  "$venv/bin/python" -m amv_gn_bridge.cli selftest >>"$log" 2>&1 \
    || fail "Built-in selftest failed (see $log)"
  ok "Verified amv-gn-bridge $tag (selftest passed)"

  local launcher="$root/start.sh"
  cat >"$launcher" <<EOF
#!/usr/bin/env bash
exec "$venv/bin/python" -m amv_gn_bridge.cli "\$@"
EOF
  chmod +x "$launcher"
  ln -sf "$launcher" "$HOME/.local/bin/amv-gn-bridge"
  ok "Launcher installed: $launcher (linked to ~/.local/bin/amv-gn-bridge)"

  if [ "${AMV_NO_LAUNCH:-0}" != "1" ]; then
    info "Starting gateway & dashboard on http://127.0.0.1:11501/dashboard ..."
    nohup "$launcher" dashboard >/dev/null 2>&1 &
    ok "amv-gn-bridge started"
  fi
}

main "$@"
