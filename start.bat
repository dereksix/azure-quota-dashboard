@echo off
REM Azure Quota Dashboard - Windows launcher.
REM First run: creates a virtualenv and installs dependencies.
REM Every run: starts the dashboard at http://127.0.0.1:8765/

setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "VENV_PY=backend\.venv\Scripts\python.exe"
set "PY_LAUNCHER="

REM ---- Locate a working Python interpreter for first-time setup ----
if not exist "%VENV_PY%" (
  echo [setup] Looking for Python...
  py -3 --version >nul 2>&1
  if !errorlevel! EQU 0 (
    set "PY_LAUNCHER=py -3"
  ) else (
    python --version >nul 2>&1
    if !errorlevel! EQU 0 (
      set "PY_LAUNCHER=python"
    ) else (
      python3 --version >nul 2>&1
      if !errorlevel! EQU 0 (
        set "PY_LAUNCHER=python3"
      )
    )
  )

  if "!PY_LAUNCHER!"=="" (
    echo.
    echo [error] Python 3.10+ not found on PATH.
    echo         Install from https://www.python.org/downloads/ and re-run this script.
    echo         During install, check "Add Python to PATH".
    echo.
    pause
    exit /b 1
  )

  echo [setup] Using: !PY_LAUNCHER!
  echo [setup] Creating virtualenv at backend\.venv ...
  !PY_LAUNCHER! -m venv backend\.venv
  if !errorlevel! NEQ 0 (
    echo.
    echo [error] Failed to create virtualenv.
    pause
    exit /b 1
  )

  echo [setup] Installing dependencies ^(this may take a minute^) ...
  "%VENV_PY%" -m pip install --upgrade pip >nul
  "%VENV_PY%" -m pip install -r backend\requirements.txt
  if !errorlevel! NEQ 0 (
    echo.
    echo [error] Failed to install dependencies. Output above.
    pause
    exit /b 1
  )
)

echo.
echo ============================================================
echo  Azure Quota Dashboard
echo  http://127.0.0.1:8765/
echo  Press Ctrl+C to stop.
echo ============================================================
echo.

"%VENV_PY%" backend\server.py
set "EXITCODE=!errorlevel!"

if !EXITCODE! NEQ 0 (
  echo.
  echo [error] Server exited with code !EXITCODE!.
  pause
)

endlocal
