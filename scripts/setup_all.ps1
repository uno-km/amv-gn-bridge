# Copyright (c) 2026 Eunho Kim / AMEVA Foundation. All rights reserved.
# SPDX-License-Identifier: Apache-2.0 OR Commercial-License
# One-click installer & updater for amv-gn-bridge (Windows).
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/uno-km/amv-gn-bridge/main/scripts/setup_all.ps1 | iex"
# Optional env vars:
#   AMV_GN_REPO    - GitHub repo (default: uno-km/amv-gn-bridge)
#   AMV_EXE_PATH   - Custom target EXE path
#   AMV_NO_LAUNCH  - Set to "1" to skip launching the GUI after install (useful for headless/AI installs)
#   AMV_WAIT_PID   - Wait for an existing PID to exit before replacing the EXE

$ErrorActionPreference = "Stop"
$Repo = if ($env:AMV_GN_REPO) { $env:AMV_GN_REPO } else { "uno-km/amv-gn-bridge" }
$InstallDir = Join-Path $env:LOCALAPPDATA "amv-gn-bridge"
$TargetExe = if ($env:AMV_EXE_PATH) { $env:AMV_EXE_PATH } else { Join-Path $InstallDir "amv-gn-bridge.exe" }
$TargetDir = Split-Path -Parent $TargetExe

Write-Host "[amv-gn-bridge] Checking latest GitHub Release from $Repo..." -ForegroundColor Cyan

if ($env:AMV_WAIT_PID -and $env:AMV_WAIT_PID -match '^\d+$') {
    Write-Host "[amv-gn-bridge] Waiting for previous process PID $($env:AMV_WAIT_PID) to exit..."
    for ($i = 0; $i -lt 40; $i++) {
        if (-not (Get-Process -Id ([int]$env:AMV_WAIT_PID) -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
}

$Headers = @{ "Accept" = "application/vnd.github+json"; "User-Agent" = "amv-gn-bridge-setup/1.0" }
$Release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers $Headers
$ExeAsset = $Release.assets | Where-Object { $_.name -eq "amv-gn-bridge.exe" } | Select-Object -First 1
if (-not $ExeAsset) {
    throw "No amv-gn-bridge.exe asset found in release $($Release.tag_name)"
}

New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
$TempExe = "$TargetExe.download"
Write-Host "[amv-gn-bridge] Downloading $($Release.tag_name) ($($ExeAsset.name))..." -ForegroundColor Green
Invoke-WebRequest -Uri $ExeAsset.browser_download_url -OutFile $TempExe -UseBasicParsing

$ShaAsset = $Release.assets | Where-Object { $_.name -eq "amv-gn-bridge.exe.sha256" } | Select-Object -First 1
if ($ShaAsset) {
    $ShaText = (Invoke-WebRequest -Uri $ShaAsset.browser_download_url -UseBasicParsing).Content.Trim()
    if ($ShaText -match '([a-fA-F0-9]{64})') {
        $Expected = $Matches[1].ToLower()
        $Actual = (Get-FileHash -Path $TempExe -Algorithm SHA256).Hash.ToLower()
        if ($Actual -ne $Expected) {
            Remove-Item -Force $TempExe -ErrorAction SilentlyContinue
            throw "SHA-256 verification failed! expected=$Expected actual=$Actual"
        }
        Write-Host "[amv-gn-bridge] SHA-256 verified OK." -ForegroundColor Green
    }
}

Move-Item -Path $TempExe -Destination $TargetExe -Force

# Download and unpack browser extension if present in release assets
$ExtAsset = $Release.assets | Where-Object { $_.name -eq "amv-gn-bridge-extension.zip" } | Select-Object -First 1
if ($ExtAsset) {
    $ExtZip = Join-Path $TargetDir "amv-gn-bridge-extension.zip"
    $ExtDir = Join-Path $TargetDir "extension"
    try {
        Invoke-WebRequest -Uri $ExtAsset.browser_download_url -OutFile $ExtZip -UseBasicParsing
        if (Test-Path $ExtDir) { Remove-Item -Recurse -Force $ExtDir }
        Expand-Archive -Path $ExtZip -DestinationPath $ExtDir -Force
        Remove-Item -Force $ExtZip -ErrorAction SilentlyContinue
        Write-Host "[amv-gn-bridge] Browser extension unpacked to $ExtDir" -ForegroundColor Green
    } catch {
        Write-Host "[amv-gn-bridge] Warning: Could not unpack browser extension: $_" -ForegroundColor Yellow
    }
}

# Add install directory to user PATH idempotently so CLI / MCP clients can invoke amv-gn-bridge.exe directly
try {
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $Entries = @($UserPath -split ';' | Where-Object { $_ -and $_.Trim() -ne "" })
    if (-not ($Entries | Where-Object { $_.TrimEnd('\') -ieq $TargetDir.TrimEnd('\') })) {
        $NewPath = ($Entries + $TargetDir) -join ';'
        [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
        Write-Host "[amv-gn-bridge] Added $TargetDir to User PATH." -ForegroundColor Green
    }
} catch {
    Write-Host "[amv-gn-bridge] Note: Could not update User PATH automatically: $_" -ForegroundColor Yellow
}

Write-Host "[amv-gn-bridge] Installed $($Release.tag_name) to $TargetExe" -ForegroundColor Cyan
if ($env:AMV_NO_LAUNCH -ne "1") {
    Write-Host "[amv-gn-bridge] Launching desktop GUI & gateway (:11501)..." -ForegroundColor Cyan
    Start-Process -FilePath $TargetExe
}
