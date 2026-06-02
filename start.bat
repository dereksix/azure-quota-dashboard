@echo off
REM Azure Quota Dashboard — quick-start launcher (Windows).
REM Creates a venv on first run, installs deps, then starts the server.

setlocal
cd /d "%~dp0"

if not exist "backend\.venv\Scripts\python.exe" (
  echo [setup] Creating Python virtualenv...
  py -3 -m venv backend\.venv || python -m venv backend\.venv
  echo [setup] Installing dependencies...
  backend\.venv\Scripts\python.exe -m pip install -q -r backend\requirements.txt
)

echo [run] Starting Azure Quota Dashboard at http://127.0.0.1:8765/
backend\.venv\Scripts\python.exe -m uvicorn server:app --app-dir backend --host 127.0.0.1 --port 8765
endlocal
