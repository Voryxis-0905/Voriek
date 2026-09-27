# Security policy

## Supported versions

Security fixes are applied to the current `main` branch. This project is currently a local-first MVP and does not provide hosted accounts or a managed cloud service.

## Local data and API keys

Provider keys configured in the UI are stored in Voriek's local data directory, normally `data/`. World-specific overrides are stored beside that world's local data. The repository ignores `data/`, `.env`, and related local configuration files.

You can set `STORY_ENGINE_DATA_DIR` to keep all local worlds, saves, and runtime configuration outside the repository. Do not attach API keys, personal save data, or exported runtime configuration to a public issue.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private security advisory flow for this repository. If that option is unavailable, contact the repository owner privately and include:

- a short impact summary;
- clear reproduction steps or a minimal proof of concept;
- affected version or commit; and
- any suggested mitigation.

Please allow time for a fix before publishing details. We will acknowledge a report, assess scope, and publish a fix or mitigation when appropriate.
