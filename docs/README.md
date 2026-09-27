# Voriek documentation

Read these documents in order:

1. [README](../README.md) — setup, local development, and validation.
2. [Design principles](DESIGN-PRINCIPLES.md) — player and creator authority, world knowledge, events, and checkpoints.
3. [Codebase map](CODEBASE.md) — module responsibilities and where to begin making a change.
4. [Contribution guide](../CONTRIBUTING.md) — change workflow and required checks.
5. [Action effects reference](Architecture/ACTION_EFFECTS.md) — the whitelist bridge that turns declared action outcomes into committed world state.

## Documentation policy

This directory contains only current specifications and implementation references. Superseded plans, agent-session notes, generated task reports, and duplicated research documents were removed from the working tree; their history remains available through Git.

When behavior changes, update the relevant design documentation in the same pull request. When code moves, update the codebase map.
