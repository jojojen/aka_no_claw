@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [setup] .venv not found, creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create .venv. Make sure Python 3.12+ is installed.
    pause
    exit /b 1
  )
)

echo [setup] Syncing Python dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
if errorlevel 1 (
  echo [ERROR] pip install failed.
  pause
  exit /b 1
)

echo [setup] Ensuring Playwright Chromium is installed...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
  echo [ERROR] playwright install chromium failed.
  pause
  exit /b 1
)

echo [setup] Done.

echo [startup] Checking for existing telegram-poll bot instances...
powershell -NoProfile -Command "& {Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'openclaw_adapter.*telegram-poll' -and $_.ProcessId -ne $PID } | ForEach-Object { Write-Host ('[startup] Stopping existing bot PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }}"

".venv\Scripts\python.exe" -m openclaw_adapter telegram-poll --with-reputation-agent --no-dashboard %*
