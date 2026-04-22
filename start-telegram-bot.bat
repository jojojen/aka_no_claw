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
  echo [setup] Installing dependencies...
  ".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
  if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
  )
  echo [setup] Done.
)

".venv\Scripts\python.exe" -m openclaw_adapter telegram-poll --with-reputation-agent %*
