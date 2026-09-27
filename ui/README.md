# Voriek UI

The Voriek interface is built with React, TypeScript, and Vite.

From the repository root:

```powershell
npm --prefix ui ci
npm run dev
```

Useful commands:

```powershell
npm --prefix ui run test
npm --prefix ui run test:e2e
npm --prefix ui run build
npm --prefix ui run lint
```

The UI talks to the local FastAPI backend at port 8000 during development. See the root [README](../README.md) for setup and validation details.
