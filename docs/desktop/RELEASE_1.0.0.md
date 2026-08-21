# AICA Desktop Release 1.0.0

**FINAL RELEASE STATUS: READY FOR DEMO**

| Field | Value |
|-------|--------|
| Product | AICA — Financial Intelligence |
| Desktop version | **1.0.0** |
| Installer filename | `AICA_Setup_1.0.0.exe` |
| Installer size | ~**216.8 MB** (227.3 MB on disk) |
| Build timestamp (this machine) | 21 Aug 2026, ~18:31 IST |
| Architecture | Windows x64 WebView2 shell + packaged FastAPI engine |

## Installer locations

- Project: `dist/AICA_Setup_1.0.0.exe`
- Desktop copy: `%USERPROFILE%\Desktop\AICA_Setup_1.0.0.exe`

## Architecture

```
AICA.exe (WebView2, no console)
    → waits for http://127.0.0.1:<port>/health
    → opens existing AICA Jinja UI
AICA.Engine.exe (PyInstaller, no console)
    → FastAPI + OpenCV + YOLO + Gemini (server-side)
    → config from %AppData%\AICA\config.env
```

One shared AICA codebase serves:

1. **Web development** — `venv` + `uvicorn` + project `.env`
2. **Windows desktop** — installer above

## Verified features (release audit)

- Desktop launcher starts packaged engine and waits for `/health`
- Signup / login / logout / session persistence
- Dashboard, POS, expenses, GST, AI Optimization
- Camera power API; YOLO `model_ready` with packaged weights
- IRA / Gemini live response through `/api/assistant`
- Clean shutdown; no orphan `AICA.Engine.exe` after close
- Silent install layout under `%LOCALAPPDATA%\AICA_DesktopTest`
- Installed engine health + login without system Python
- Web `uvicorn` health + login still HTTP 200
- No `.env` / session secret files inside the engine package
- Secret-pattern scan of packaged text assets: no live API keys or DB URLs found
- Specs use `console=False` (no Python/Uvicorn console for end users)
- Installer is a single portable EXE (copyable to another Windows machine)

## Installation instructions

1. Copy `AICA_Setup_1.0.0.exe` to the target PC.
2. Run the installer (admin may be required for Program Files).
3. Ensure **WebView2 Runtime** is present (usually already on Windows 11).
4. Edit `%AppData%\AICA\config.env` and set real values:

```env
DATABASE_URL=postgresql://USER:PASSWORD@YOUR_HOST:5432/aica_db
GEMINI_API_KEY=your_server_side_key
```

5. Launch **AICA** from the Start Menu.
6. Sign up or log in; use POS / camera / IRA as usual.

Do **not** leave `USER` / `PASSWORD` / `HOST` template placeholders — AICA ignores them and will refuse to start.

## Development vs production configuration

| | Development (web) | Desktop production |
|--|-------------------|--------------------|
| App | `uvicorn backend.main:app` | `AICA.exe` + `AICA.Engine.exe` |
| Config | project `.env` | `%AppData%\AICA\config.env` |
| Database | local PostgreSQL typical | hosted PostgreSQL recommended |
| Gemini | `.env` `GEMINI_API_KEY` | AppData `GEMINI_API_KEY` |
| Logs | terminal | `%AppData%\AICA\logs\` |

Precedence: process environment → AppData `config.env` (valid only) → project `.env` → no fake desktop fallback.

## Database requirements

- PostgreSQL compatible with existing AICA SQLAlchemy schema (same org isolation).
- Desktop does **not** require end users to install PostgreSQL locally if a hosted URL is provided.
- Never hardcode credentials into the installer or Git.

## Gemini / IRA requirements

- Key remains **server-side only** (never in frontend JS or the installer payload as a baked-in secret).
- Fail-fast timeouts/backoff: if Gemini is down or quota-exhausted, IRA returns a controlled message; POS/accounting continue.
- For demo: set a valid `GEMINI_API_KEY` in AppData (or project `.env` for web).

## Known limitations

- First engine start can take tens of seconds (Torch/YOLO load).
- Installer ~217 MB due to OpenCV/Torch/Ultralytics.
- Hosted PostgreSQL must be configured by the operator for machines without local Postgres.
- In-app auto-update is **not** implemented yet (see below).
- Camera/mic require Windows privacy permissions for desktop apps.
- Clean-machine validation was performed via silent install + packaged engine probes on this developer PC; a second physical PC still needs the operator to supply production `DATABASE_URL` / Gemini.

## Future update strategy

1. Bump `AICA_VERSION` / Inno `#define MyAppVersion` / `desktop/config/version.json`.
2. `.\desktop\scripts\build_installer.ps1`
3. Publish `AICA_Setup_x.y.z.exe` on GitHub Releases.
4. Later: safe optional updater (never force-overwrite user AppData secrets).

Version sequence: `1.0.0` → `1.1.0` → `1.2.0` → `2.0.0`.

## Rebuild commands (maintainers)

```powershell
# Full installer (engine + launcher + Inno)
.\desktop\scripts\build_installer.ps1

# Verification helpers
.\desktop\scripts\e2e_desktop_shell.py   # via: python desktop\scripts\e2e_desktop_shell.py
python desktop\scripts\final_release_audit.py
python desktop\scripts\final_smoke.py
```

## Release decision

**READY FOR DEMO** — installer present, critical desktop paths verified, secrets not embedded in the package, web workflow intact. Provide AppData DB + Gemini config on each demo machine before launch.
