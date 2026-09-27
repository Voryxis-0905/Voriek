# Contributing to Voriek

Voriek is an actively developed MVP. Start with the [README](README.md), [design principles](docs/DESIGN-PRINCIPLES.md), and [codebase map](docs/CODEBASE.md).

## Setup

Use Python 3.12 and Node.js 24. Create `.venv`, install `requirements.lock.txt`, and run `npm --prefix ui ci` as described in the README. Activate the Python environment before running npm commands that execute backend tests:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
npm run dev
```

On macOS/Linux, use `source .venv/bin/activate`. If PowerShell scripts are restricted, invoke `.venv\Scripts\python.exe backend/run_tests.py` directly instead of activating the environment.

## Making a change

1. Create a branch for one focused issue and describe the before/after behavior.
2. Change the module that owns the responsibility; do not add new behavior to compatibility exports.
3. For behavior bugs, add an independent regression test in `backend/tests/`. Avoid order-dependent scenarios in the legacy script.
4. Update documentation when changing a flow, API, or persisted data format.
5. Run `npm run check` before opening a pull request and report validation results and limits.

`npm run check` runs backend tests, legacy checks, Vitest UI tests, the TypeScript/UI build, and linting. You can run `npm test`, `npm run test:legacy`, `npm run test:ui`, `npm run test:e2e`, `npm run build`, or `npm run lint` separately. CI runs the backend checks on Windows and Linux, and UI checks on Linux. E2E requires Chromium, installed with `npm --prefix ui exec -- playwright install --with-deps chromium`.

## Data and behavior changes

- Never commit API keys, `.env` files, personal world/save data, `.venv`, `node_modules`, or `dist`.
- To reproduce a data issue, create a minimal sanitized fixture and point `STORY_ENGINE_DATA_DIR` to an isolated directory.
- Preserve compatibility with existing saves, or document a migration path. Prompt changes are behavior changes and need their own evaluation.
- Automated tests must not require API keys or call live AI providers.

This repository is licensed under the MIT License — see [LICENSE](LICENSE). Before publishing new releases, review sample data and keep release documentation current.
