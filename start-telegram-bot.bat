@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo .venv\Scripts\python.exe not found.
  echo Create the virtual environment first:
  echo   python -m venv .venv
  echo   .\.venv\Scripts\Activate.ps1
  echo   python -m pip install -r requirements-dev.txt
  exit /b 1
)

set TELEGRAM_ARGS=%*

".venv\Scripts\python.exe" -m openclaw_adapter telegram-poll %TELEGRAM_ARGS%
