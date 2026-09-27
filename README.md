# Voriek

Voriek is a local interactive storytelling application by Voryxis, built with a Python/FastAPI backend and a React/TypeScript interface.

![Voriek interactive storytelling interface](docs/assets/voriek-preview.png)

Players choose actions that shape outcomes. Each world retains its own history, rules, characters, memories, map, calendar, quests, and consequences. A creator can also play the world while using dedicated tools to inspect and edit its state.

Inventory uses a stable, genre-neutral schema with flexible descriptions and effects. The map previews routes, travel time, and risk; travel can be narrated as a transition, time skip, or journey while the engine advances the world clock and events deterministically.

## Project status

This is an actively developed MVP. Generated worlds use advisory checkpoints: a checkpoint represents an event that can unfold in different ways, rather than a location or outcome that traps the player. The engine protects continuity by checking narrative time, location, declared consequences, and explicit player stopping points before committing a turn.

See the [design principles](docs/DESIGN-PRINCIPLES.md), [codebase map](docs/CODEBASE.md), and [action effects reference](docs/Architecture/ACTION_EFFECTS.md) for the current direction and implementation detail.

## Requirements

Python 3.12 and Node.js 24 were used to validate this repository.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
npm --prefix ui ci
```

On Windows, run `Start-Voriek.cmd`, then open http://localhost:5173. The backend runs at http://127.0.0.1:8000. Press Ctrl+C in the running window to stop it.

On other operating systems, activate the Python environment and run `npm run dev`.

Configure a provider and model in Settings when you want live AI generation. Runtime configuration, API keys, personal worlds, and saves stay outside Git. The repository contains no personal play data; the application creates its data directory at startup.

For a repeatable local setup, copy `.env.example` to `.env` and set only the values you need. The application loads `.env` at startup; the file is ignored by Git. Settings can override the default provider configuration for a world.

## Validation

```powershell
npm run check
npm run test:e2e
```

`npm run check` runs backend tests, the legacy compatibility suite, UI tests, the production UI build, and linting. The test runner uses temporary world data and blocks provider HTTP calls.

Do not run `backend/test_engine.py` directly against a world you are playing. It is a legacy diagnostic script that mutates test worlds and runtime configuration. Use `npm run test:legacy` instead.

Set `STORY_ENGINE_DATA_DIR` to choose a separate local data directory. Without it, the application uses `data/`.

## Repository layout

- `backend/` — API, engine, rules, memory, and AI pipeline.
- `ui/` — web interface.
- `docs/` — current design documentation and implementation references.
- `data/` — created locally and excluded from Git.

Read the [documentation index](docs/README.md) and [contribution guide](CONTRIBUTING.md) before changing the project.

## License

Voriek is licensed under the [MIT License](LICENSE).
