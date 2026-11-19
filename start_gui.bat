@echo off
cd /d "%~dp0"

REM ASCII only: safely launch amv-gn-bridge desktop GUI + :11501 gateway.
python -c "import sys" >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python 3.10+ runtime was not detected.
    pause
    exit /b 1
)

set "PYTHONPATH=%CD%;%PYTHONPATH%"
python -c "import customtkinter, fastapi, uvicorn, playwright" >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Required Python packages are missing. Run: pip install -e .
    pause
    exit /b 1
)

set "PY_GUI="
for /f "usebackq delims=" %%P in (`python -c "import sys, os; pw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'); print(pw if os.path.isfile(pw) else sys.executable)"`) do set "PY_GUI=%%P"
if "%PY_GUI%"=="" set "PY_GUI=python"

start "amv-gn-bridge" "%PY_GUI%" -m amv_gn_bridge.cli
exit /b 0
