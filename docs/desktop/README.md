# AICA Desktop Application

Professional Windows distribution layer around the **existing** AICA V1.0 web application.

```
AICA.exe (WebView2 launcher)
        │
        ▼
  127.0.0.1:<port>
        │
        ▼
AICA.Engine.exe (packaged FastAPI + OpenCV + YOLO)
        │
        ▼
Hosted PostgreSQL + Gemini (keys in %AppData%\AICA\config.env)
```

The **web development workflow is unchanged**: use `venv` + `uvicorn` + local `.env` + local PostgreSQL.

## Architecture

| Layer | Role |
|-------|------|
| Shared AICA code | FastAPI, Jinja, SQLAlchemy, POS, tax, IRA, YOLO |
| Desktop launcher | Starts engine, opens WebView2, clean shutdown |
| Packaged engine | PyInstaller onedir with templates/static/weights |
| Installer | Inno Setup → `AICA_Setup_1.0.0.exe` |
| AppData | `%AppData%\AICA\config.env`, logs, session secret |

## Prerequisites (developers building the installer)

1. Windows 10/11 x64
2. Project `venv` with `requirements.txt`
3. `pip install pyinstaller pywebview`
4. Optional: [Inno Setup 6](https://jrsoftware.org/isinfo.php) for `AICA_Setup_*.exe`
5. WebView2 Runtime on target PCs (usually already present on Win11)

## Build

From repo root (PowerShell):

```powershell
.\desktop\scripts\build_installer.ps1
```

Or stepwise:

```powershell
.\desktop\scripts\build_engine.ps1
.\desktop\scripts\build_launcher.ps1
```

Outputs:

- `dist/AICA.exe` — desktop shell
- `dist/AICA.Engine/` — FastAPI+CV engine
- `dist/AICA_Setup_1.0.0.exe` — installer (if Inno Setup installed)

## Run desktop (development, without packaging)

```powershell
# Terminal A — normal web engine
.\venv\Scripts\uvicorn.exe backend.main:app --host 127.0.0.1 --port 8000

# Terminal B — WebView shell against that port
$env:AICA_PORT="8000"
.\venv\Scripts\python.exe -m desktop.launcher.main
```

Or let the launcher start uvicorn itself:

```powershell
.\venv\Scripts\python.exe -m desktop.launcher.main
```

## Production configuration

After install, edit:

`%AppData%\AICA\config.env`

```
DATABASE_URL=postgresql://USER:PASSWORD@your-real-host:5432/aica_db
GEMINI_API_KEY=...
```

Use real hostnames only. Template values like `HOST` / `USER:PASSWORD@HOST` are **ignored** and AICA will show a configuration error instead of connecting.

**Precedence:** process environment → `%AppData%\AICA\config.env` → project `.env` (dev) → no fake desktop fallback.

**Never** put secrets in Git, the installer, or frontend JS.

Local packaged demo (developer machine): with a scrubbed AppData config, `dist\AICA.exe` can pick up the repo `.env` when run from `dist\` next to the project. Installed copies on other PCs must use AppData only.

## YOLO / camera

- Weights packaged from `vision/weights/aica_product_detector.pt`
- Camera stays OFF until toggled in POS (unchanged)
- OpenCV runs inside the engine process

## IRA / Gemini

- Keys stay server-side (`GEMINI_API_KEY` in `.env` or `%AppData%\AICA\config.env`).
- Requests use a hard timeout (`AICA_GEMINI_TIMEOUT_S`, default 20s), limited retries with exponential backoff, and at most two models on quota errors.
- `/api/assistant` also has an overall wall-clock limit (`AICA_IRA_TIMEOUT_S`, default 25s).
- If Gemini is unavailable or quota-exhausted, IRA returns:  
  `IRA is temporarily unavailable. Please try again later.`  
  The rest of AICA (POS, camera, accounting) continues normally.
- Verification treats Gemini quota as an **external-service limitation**, not a desktop packaging failure.

## Verification status (developer machine)

Automated checks covered by:

- `desktop/scripts/verify_packaged_engine.py` — packaged engine APIs
- `desktop/scripts/e2e_desktop_shell.py` — `AICA.exe` launches engine, modules, IRA, clean shutdown
- `desktop/scripts/test_silent_install.ps1` — Inno silent install to `%LOCALAPPDATA%\AICA_DesktopTest`

IRA/Gemini requires a valid `GEMINI_API_KEY` in `%AppData%\AICA\config.env` (or project `.env` for web/dev).

## Release process (future updates)

1. Bump `AICA_VERSION` / `desktop/config/version.json` / Inno `#define MyAppVersion`.
2. `.\desktop\scripts\build_installer.ps1`
3. Publish `dist\AICA_Setup_x.y.z.exe` to GitHub Releases.
4. In-app auto-update is not forced yet — structure is ready for a later safe updater.
