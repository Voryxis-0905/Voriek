@echo off
setlocal
cd /d "%~dp0"
set "PATH=%~dp0.venv\Scripts;%PATH%"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment is missing. See README-LOCAL.md.
  pause
  exit /b 1
)
echo Voriek: http://localhost:5173
echo Keep this window open. Press Ctrl+C to stop both services.
call npm.cmd run dev
pause
