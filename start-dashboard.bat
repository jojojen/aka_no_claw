@echo off
setlocal

pushd "%~dp0" >nul

if not exist ".venv\Scripts\python.exe" (
  echo [setup] .venv not found, creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create .venv. Make sure Python 3.12+ is installed.
    pause
    popd >nul
    exit /b 1
  )
  echo [setup] Installing dependencies...
  ".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
  if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause
    popd >nul
    exit /b 1
  )
  echo [setup] Done.
)

set "DASHBOARD_ARGS=%*"
if "%DASHBOARD_ARGS%"=="" set "DASHBOARD_ARGS=--open-browser"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ensure-dashboard-port.ps1" %DASHBOARD_ARGS%
if errorlevel 1 (
  popd >nul
  exit /b 1
)

".venv\Scripts\python.exe" -m openclaw_adapter serve-dashboard %DASHBOARD_ARGS%
set "EXIT_CODE=%ERRORLEVEL%"

popd >nul
exit /b %EXIT_CODE%
