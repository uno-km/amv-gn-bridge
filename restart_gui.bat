@echo off
cd /d "%~dp0"

REM ASCII only: stop existing amv-gn-bridge desktop/gateway instances and relaunch GUI.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -match 'amv_gn_bridge\.cli' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>nul
timeout /t 1 /nobreak >nul
call "%~dp0start_gui.bat"
exit /b 0
