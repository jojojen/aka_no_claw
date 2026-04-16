@echo off
setlocal

pushd "%~dp0" >nul

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Cannot find .venv\Scripts\python.exe
  echo.
  echo Please create the virtual environment first:
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
  popd >nul
  exit /b 1
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
