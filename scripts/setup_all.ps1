# Copyright (c) 2026 Eunho Kim / AMEVA Foundation. All rights reserved.
# SPDX-License-Identifier: Apache-2.0 OR Commercial-License
# One-click installer & updater for amv-gn-bridge (Windows).
# Usage: irm https://raw.githubusercontent.com/uno-km/amv-gn-bridge/main/scripts/setup_all.ps1 | iex

$ErrorActionPreference = "Stop"
$Repo = if ($env:AMV_GN_REPO) { $env:AMV_GN_REPO } else { "uno-km/amv-gn-bridge" }
$InstallDir = Join-Path $env:LOCALAPPDATA "amv-gn-bridge"
$TargetExe = if ($env:AMV_EXE_PATH) { $env:AMV_EXE_PATH } else { Join-Path $InstallDir "amv-gn-bridge.exe" }

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

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $TargetExe) | Out-Null
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
Write-Host "[amv-gn-bridge] Installed to $TargetExe — Launching..." -ForegroundColor Cyan
Start-Process -FilePath $TargetExe
