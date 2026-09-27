# Voriek local setup notes

This file records local-machine setup notes from the initial import on 17 September 2026. It is not the current project specification; use [README.md](README.md) for installation and validation.

## Run locally

- Open `Start-Voriek.cmd` and keep that window running, then visit http://localhost:5173.
- Press Ctrl+C in the running window to stop the backend and UI.
- Do not start another development session when the application is already listening on ports 8000 and 5173.
- Dependencies live in `.venv`, based on the machine's Python 3.12 runtime. Recreate `.venv` when moving to another machine.
- Install UI dependencies with `npm ci` using `ui/package-lock.json`.
- `requirements.lock.txt` records the installed Python versions; `requirements.txt` remains the source dependency list.

## Historical validation record

The initial import compiled successfully, the local health and world endpoints returned HTTP 200, and the UI built successfully. The details are historical and may be stale; run `npm run check` for the current result.

Local worlds and configuration imported from the original archive are preserved. Tests use isolated temporary data and must not run against personal worlds.

## Reinstall

From the project root, create `.venv` with Python 3.12 and run `.venv\Scripts\python.exe -m pip install -r requirements.lock.txt`. Run `npm ci` in `ui`. Do not copy `.venv` between machines.
