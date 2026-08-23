# AICA Auto Updater — Architecture Plan

**Status:** Analysis and planning only (post v1.0.2 release)  
**Date:** 2026-08-23  
**Scope:** Safe Windows desktop auto-update for AICA packaged with PyInstaller + Inno Setup

---

## 1. Executive Summary

AICA v1.0.2 is a **WebView2 launcher + FastAPI engine** desktop app installed per-user at `%LOCALAPPDATA%\AICA\`. User data lives separately under `%APPDATA%\AICA\` and `%LOCALAPPDATA%\AICA\webview\`. **No in-app updater exists today**, but the project already declares `"update.strategy": "github_releases"` in `desktop/config/version.json` and ships an Inno Setup installer designed for in-place upgrades via a stable `AppId`.

**Recommended architecture:** **GitHub Releases + signed manifest JSON (Option B) + separate `AICA.Updater.exe` (Option E) + Inno Setup silent reinstall (Option D).**

The running `AICA.exe` performs a **non-blocking background check** against a release manifest. When the user accepts an update, a **small external updater process** downloads and verifies the official `AICA_Setup_x.y.z.exe`, waits for AICA to exit, runs the installer silently, optionally preserves a rollback backup, and relaunches AICA.

This fits the existing packaging model with **minimal structural change** — no custom update server, no self-replacing EXE, and no overwrite of user Roaming data.

---

## 2. Repository Evidence — Current State

### 2.1 Application entry points

| Component | Entry | Packaging |
|-----------|-------|-----------|
| **Launcher** | `desktop/launcher/main.py` | PyInstaller onefile → `dist/AICA.exe` |
| **Engine** | `desktop/engine_entry.py` → uvicorn `backend.main:app` | PyInstaller onedir → `dist/AICA.Engine/` |
| **Backend API** | `backend/main.py` | Bundled inside engine `_internal/` |

Launcher responsibilities (`desktop/launcher/main.py`):

- Resolve engine at `%LOCALAPPDATA%\AICA\AICA.Engine.exe` (flattened Inno layout)
- Start engine subprocess, poll `GET /health` until ready (up to 180s)
- Open WebView2 window to `http://127.0.0.1:<port>/login`
- Register `atexit` + window close handler to stop engine

### 2.2 Process model

```
AICA.exe (launcher, parent)
  ├── subprocess.Popen → AICA.Engine.exe (child)
  └── pywebview WebView2 → http://127.0.0.1:<port>/
        └── js_api: DesktopVoiceBridge (voice_bridge.py)
```

Engine stop (`_stop_engine` in `main.py`):

- `proc.terminate()` → wait 8s → `proc.kill()` if needed
- No HTTP graceful shutdown to uvicorn; FastAPI `shutdown_event` stops camera only
- E2E test (`e2e_desktop_shell.py`) expects zero orphan `AICA.Engine.exe` after shutdown

### 2.3 Packaging and build output

| Script | Output |
|--------|--------|
| `desktop/scripts/build_engine.ps1` | `dist/AICA.Engine/AICA.Engine.exe` + `_internal/` |
| `desktop/scripts/build_launcher.ps1` | `dist/AICA.exe` |
| `desktop/scripts/build_installer.ps1` | `dist/AICA_Setup_{version}.exe` |

**Installed layout** (`desktop/packaging/aica_setup.iss`):

```
%LOCALAPPDATA%\AICA\
  AICA.exe
  AICA.Engine.exe
  _internal\          ← engine bundle (frontend, vision weights, Python libs)
  version.json
  config.env.example
  README_CONFIG.txt
  unins000.exe        ← after install
```

**Desktop shortcut:** `{userdesktop}\AICA.lnk` → `{app}\AICA.exe`  
**Start Menu:** `{group}\AICA`, `{group}\Uninstall AICA`

### 2.4 Inno Setup behavior (upgrade-ready)

Key facts from `aica_setup.iss`:

- **AppId:** `{A1CA1000-2026-4A1C-9F10-AICASETUP1000}` — stable across versions
- **Install dir:** `{localappdata}\AICA` (per-user, `PrivilegesRequired=lowest`)
- **Files:** all use `Flags: ignoreversion` — binaries replaced on upgrade
- **CloseApplications:** `yes` — installer can close running AICA
- **Post-install:** creates `%APPDATA%\AICA\`, `%LOCALAPPDATA%\AICA\webview\`
- **config.env:** copied from example **only if missing** — existing secrets preserved
- **Silent flags** (verified pattern in `test_silent_install.ps1`): `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`

**Note:** `test_silent_install.ps1` expects legacy `engine\AICA.Engine.exe` layout; current Inno flattens to `AICA.Engine.exe` at install root. Update test script during implementation.

### 2.5 Version metadata — current sources

| Location | Current value | Synced by build? |
|----------|---------------|------------------|
| `desktop/config/version.json` | `1.0.2` | Yes — `build_installer.ps1` updates version + build |
| `desktop/scripts/build_installer.ps1` | `$Version = "1.0.2"` | Master bump (manual duplicate) |
| `desktop/packaging/aica_setup.iss` | `#define MyAppVersion "1.0.2"` | **Not synced** — manual |
| `backend/runtime_paths.py` | `APP_VERSION` fallback `"1.0.2"` | **Not synced** — manual |
| `desktop/packaging/pyi_rth_aica.py` | `AICA_VERSION` default `"1.0.2"` | **Not synced** — manual |
| `desktop/packaging/file_version_info.txt` | `1.0.2` | **Orphan** — not wired into PyInstaller specs |
| Git tags | `v1.0.0`, `v1.0.1`, `v1.0.2` | Manual release workflow |
| `/health` endpoint | Returns `version`, `build`, `desktop` | Runtime from env + `version.json` |
| Profile UI | `frontend/templates/profile.html` | Displays `app_version`, `app_build`, `app_channel` |

`version.json` already documents the intended strategy:

```json
"update": {
  "strategy": "github_releases",
  "notes": "Future releases publish AICA_Setup_x.y.z.exe. Same AppId upgrades %LOCALAPPDATA%\\AICA in place."
}
```

### 2.6 Existing update infrastructure

**None implemented.** Grep confirms no version-check, download, or apply-update code. Documentation in `docs/desktop/README.md` and `docs/desktop/RELEASE_1.0.0.md` describes manual GitHub Releases only. No `.github/workflows/` CI release automation exists.

### 2.7 User data vs install binaries

#### Must NEVER be overwritten by updater

| Path | Contents |
|------|----------|
| `%APPDATA%\AICA\config.env` | Secrets (`DATABASE_URL`, `GEMINI_API_KEY`, `AICA_SECRET_KEY`) |
| `%APPDATA%\AICA\aica.db` (+ `-wal`, `-shm`) | SQLite production database |
| `%APPDATA%\AICA\session_secret` | Session signing key |
| `%APPDATA%\AICA\logs\` | Engine, voice, calibration logs |
| `%APPDATA%\AICA\voice\models\personal_*` | Personal wake embeddings (user-trained) |
| `%APPDATA%\AICA\voice\models\` | Materialized voice models (refreshed by app logic, not installer) |
| `%LOCALAPPDATA%\AICA\webview\` | WebView2 profile (cookies, localStorage, Remember Me) |

#### Safe to replace (install dir only)

| Path | Contents |
|------|----------|
| `%LOCALAPPDATA%\AICA\AICA.exe` | Launcher |
| `%LOCALAPPDATA%\AICA\AICA.Engine.exe` | Engine entry |
| `%LOCALAPPDATA%\AICA\_internal\` | Engine bundle |
| `%LOCALAPPDATA%\AICA\version.json` | Shipped version metadata |
| `%LOCALAPPDATA%\AICA\config.env.example` | Template |

**Important:** Install dir and WebView profile share `%LOCALAPPDATA%\AICA\`. The updater must replace **only known binary paths** (`AICA.exe`, `AICA.Engine.exe`, `_internal\`, shipped templates) and must **not** delete `webview\`.

Roaming user data (`%APPDATA%\AICA\`) is **outside** the Inno `[Files]` section entirely — upgrades already preserve it.

### 2.8 GitHub release workflow (current)

Manual process documented in `docs/desktop/README.md`:

1. Bump version in multiple files
2. Run `build_installer.ps1`
3. Publish `AICA_Setup_x.y.z.exe` to GitHub Releases
4. Create Git tag `vx.y.z`

Repository: `https://github.com/samrudhht2005-dev/aica-project.git`

---

## 3. Architecture Options Analysis

### Option A — GitHub Releases API directly

AICA calls `GET /repos/{owner}/{repo}/releases/latest` and parses `tag_name` + installer asset URL.

| Criterion | Assessment |
|-----------|------------|
| **Advantages** | No extra hosting; uses existing GitHub infra; simple for MVP |
| **Disadvantages** | API rate limits (60/hr unauthenticated); asset naming conventions fragile; no `minimum_supported_version`, `mandatory`, or custom channel without parsing release body |
| **Security** | HTTPS + GitHub trust; must validate asset name matches `AICA_Setup_{version}.exe`; no built-in hash in API response |
| **Complexity** | Low |
| **Reliability** | Good if asset naming is strict |
| **Fits AICA** | Partial — good for version discovery, insufficient alone for integrity metadata |
| **Hosting cost** | Free |
| **Update running app** | No — still needs external process |
| **Rollback** | Not supported by API |
| **User data preserved** | Depends on install mechanism, not API |

### Option B — Update manifest JSON on GitHub Releases

Ship `aica-update-manifest.json` (or per-version `aica-update-1.0.3.json`) as a release asset or on `main` at a stable URL.

Example structure (design target):

```json
{
  "version": "1.0.3",
  "minimum_supported_version": "1.0.0",
  "channel": "stable",
  "release_notes": "Bug fixes and improvements.",
  "published_at": "2026-09-01T12:00:00Z",
  "download_url": "https://github.com/.../releases/download/v1.0.3/AICA_Setup_1.0.3.exe",
  "sha256": "...",
  "size_bytes": 227000000,
  "mandatory": false
}
```

| Criterion | Assessment |
|-----------|------------|
| **Advantages** | Structured metadata; SHA-256 in manifest; supports channels/prereleases; small fast download for check phase |
| **Disadvantages** | Must publish manifest with every release; stable URL vs per-release URL decision |
| **Security** | HTTPS + hash verification; pin manifest URL to GitHub release asset or tagged raw file |
| **Complexity** | Low–medium |
| **Reliability** | High — decouples check from large installer download |
| **Fits AICA** | **Excellent** — aligns with existing `version.json` strategy field |
| **Hosting cost** | Free (GitHub Releases) |
| **Update running app** | No — needs installer + external updater |
| **Rollback** | Can include `previous_version` metadata |
| **User data preserved** | Yes, when combined with Inno in-place upgrade |

### Option C — Custom update server/API

Dedicated endpoint (e.g. `https://updates.aica.example/manifest`).

| Criterion | Assessment |
|-----------|------------|
| **Advantages** | Full control; analytics; staged rollouts; kill switch |
| **Disadvantages** | Hosting cost; ops burden; new failure domain; overkill for current scale |
| **Security** | Requires TLS cert management, API auth design |
| **Complexity** | High |
| **Reliability** | Depends on server SLA |
| **Fits AICA** | Poor fit today — GitHub Releases already established |
| **Hosting cost** | Non-zero |
| **Update running app** | Same Windows constraints apply |
| **Rollback** | Possible server-side |
| **User data preserved** | Same as install mechanism |

### Option D — Inno Setup installer update workflow

Re-run `AICA_Setup_x.y.z.exe` with silent flags to upgrade in place.

| Criterion | Assessment |
|-----------|------------|
| **Advantages** | **Already implemented**; stable AppId; `CloseApplications=yes`; config.env preservation in `CurStepChanged` |
| **Disadvantages** | Cannot run from inside running `AICA.exe`; large download (~200+ MB); WebView2 check shows MsgBox unless suppressed |
| **Security** | Installer integrity via SHA-256 of downloaded file |
| **Complexity** | Low — reuse existing artifact |
| **Reliability** | High for Windows per-user installs |
| **Fits AICA** | **Core install mechanism** |
| **Hosting cost** | Free on GitHub Releases |
| **Update running app** | Inno closes app via `CloseApplications` — prefer explicit graceful shutdown first |
| **Rollback** | Requires separate backup step before install |
| **User data preserved** | Yes — Roaming AppData untouched; webview dir must be excluded from file replacement |

### Option E — Separate `AICA.Updater.exe`

Small PyInstaller onefile (~5–15 MB) dedicated to download, verify, wait, install, restart.

| Criterion | Assessment |
|-----------|------------|
| **Advantages** | Solves self-update problem cleanly; can show native progress UI; survives launcher exit |
| **Disadvantages** | Extra binary to build, sign, and ship; must be bundled or downloaded |
| **Security** | Smaller attack surface if minimal; should be shipped inside install dir and updated with each release |
| **Complexity** | Medium |
| **Reliability** | High — standard pattern for Windows apps |
| **Fits AICA** | **Required complement** to Options B + D |
| **Hosting cost** | None (ships with AICA) |
| **Update running app** | **Yes** — designed for this |
| **Rollback** | Can orchestrate backup/restore |
| **User data preserved** | Yes — updater only touches install dir binaries |

### Option F — Temporary PowerShell/batch script

Generate a `.ps1` or `.cmd` at update time to wait and run installer.

| Criterion | Assessment |
|-----------|------------|
| **Advantages** | No extra build artifact |
| **Disadvantages** | Execution policy issues; fragile; poor UX; hard to code-sign |
| **Fits AICA** | Not recommended |

---

## 4. Recommended Architecture

### 4.1 Selection

**Primary:** Option B (manifest JSON) + Option E (`AICA.Updater.exe`) + Option D (Inno silent upgrade)

**Secondary check path:** Option A (GitHub Releases API) as fallback if manifest fetch fails — compare `tag_name` only, still require manifest for download URL and SHA-256 before installing.

### 4.2 Why this is the best choice

1. **Matches existing project intent** — `version.json` already declares `github_releases`.
2. **Reuses proven installer** — Inno AppId in-place upgrade is already designed and tested.
3. **Solves the Windows self-update problem** — external updater waits for process exit before replacing EXEs.
4. **No new hosting** — GitHub Releases only.
5. **Security** — manifest carries SHA-256; download verified before execute.
6. **User data safety** — Inno never touches `%APPDATA%\AICA\`; updater excludes `webview\`.
7. **Non-blocking startup** — background thread fetches small JSON manifest after UI is up.

### 4.3 Component diagram

```
┌─────────────────────────────────────────────────────────────┐
│  AICA.exe (launcher)                                        │
│  ┌─────────────────┐  ┌──────────────────────────────────┐  │
│  │ UpdateChecker   │  │ WebView2 UI (chrome.html banner) │  │
│  │ (background)    │  │  or native ctypes dialog         │  │
│  └────────┬────────┘  └──────────────────────────────────┘  │
│           │ fetch manifest (timeout 5s)                     │
└───────────┼─────────────────────────────────────────────────┘
            ▼
   GitHub Releases: aica-update-manifest.json
            │
            │ user clicks "Update Now"
            ▼
┌─────────────────────────────────────────────────────────────┐
│  AICA.Updater.exe                                           │
│  1. Download AICA_Setup_x.y.z.exe → %TEMP%\AICA\updates\   │
│  2. Verify SHA-256                                          │
│  3. Signal AICA.exe to exit (or wait on PID)                │
│  4. Wait for AICA.exe + AICA.Engine.exe (timeout 30s)       │
│  5. Backup binaries → %LOCALAPPDATA%\AICA\.backup\         │
│  6. Run installer /VERYSILENT /SUPPRESSMSGBOXES /NORESTART  │
│  7. Verify new version.json                                 │
│  8. Relaunch AICA.exe                                       │
│  9. On failure → restore backup, show error                 │
└─────────────────────────────────────────────────────────────┘
            ▼
   Inno Setup (same AppId) → upgrade %LOCALAPPDATA%\AICA\
```

---

## 5. Critical Windows Update Problem

### 5.1 Problem

A running `AICA.exe` (PyInstaller onefile) and `AICA.Engine.exe` lock their executables and loaded DLLs. **AICA cannot reliably replace itself** while running. PyInstaller onefile also extracts to a temp `_MEIPASS` directory at runtime.

Additionally:

- `AICA.Engine.exe` is a child process holding file handles in `_internal\`
- WebView2 may hold handles under `%LOCALAPPDATA%\AICA\webview\`
- Inno `[Files]` replaces `_internal\` recursively — engine must be fully stopped first

### 5.2 Solution: external updater + graceful shutdown

**Do not** attempt in-process binary replacement.

**Do:**

1. Launch `AICA.Updater.exe` as a detached process (or ship it adjacent to `AICA.exe`)
2. Pass: target version, installer path or URL, parent PID, install directory
3. AICA performs graceful shutdown sequence (see §5.3)
4. Updater polls until `AICA.exe` and `AICA.Engine.exe` are gone
5. Updater runs Inno installer silently into the same `{localappdata}\AICA`

### 5.3 Exact update sequence (adapted to AICA)

```
1.  AICA running (launcher + engine + WebView2 + optional wake listener)
2.  Background UpdateChecker fetches manifest (after UI loaded, ~2s delay)
3.  Semver compare: manifest.version > app_release_info().version
4.  If update available → show notification (non-modal banner or toast)
5.  User clicks "Update Now" (or "Later" / dismiss)
6.  Launcher disables new wake/listen sessions; cancels active voice
7.  UpdateChecker downloads installer to %TEMP%\AICA\updates\{version}\
       - streaming download with progress events to UI
       - resume not required for v1 (re-download on retry)
8.  Verify SHA-256 against manifest
9.  Launch AICA.Updater.exe with:
       --installer "{path}"
       --install-dir "%LOCALAPPDATA%\AICA"
       --wait-pid {launcher_pid}
       --target-version 1.0.3
       --restart
10. AICA launcher:
       - voice.cancel_voice_listen()
       - _stop_engine() (terminate → wait 8s → kill)
       - webview.destroy() / exit main loop
11. Updater waits for PID exit (poll every 500ms, timeout 30s)
12. Updater enumerates and waits for any remaining AICA.Engine.exe (tasklist)
13. Updater copies backup:
       %LOCALAPPDATA%\AICA\ → %LOCALAPPDATA%\AICA\.backup\{current_version}\
       (exclude webview\, .backup\, logs\ if present)
14. Updater runs:
       AICA_Setup_x.y.z.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
       [/DIR="%LOCALAPPDATA%\AICA" if needed — default matches]
15. Updater reads %LOCALAPPDATA%\AICA\version.json → confirm version
16. Updater launches %LOCALAPPDATA%\AICA\AICA.exe
17. Updater exits 0
18. On any failure after step 13:
       - restore from .backup\{current_version}\
       - write %LOCALAPPDATA%\AICA\logs\update_failed.log
       - show MessageBox with error
       - exit non-zero
```

### 5.4 Why not launcher-only or installer-only?

| Approach | Verdict |
|----------|---------|
| AICA replaces its own EXE | **Unsafe** — file locks, partial writes |
| Download + exec installer from AICA then exit | **Partial** — works if AICA exits first, but no wait/restart/rollback orchestration |
| Inno `CloseApplications=yes` alone | **Risky** — may force-kill without voice/engine cleanup; no hash verify orchestration |
| Separate Updater | **Recommended** — full lifecycle control |

---

## 6. Versioning Audit and Single Source of Truth

### 6.1 Current duplication problem

Version `1.0.2` is manually duplicated in **6+ locations**. Only `version.json` build stamp is synced by `build_installer.ps1`. This creates risk:

```
Application health = 1.0.3
Installer filename = 1.0.2
Git tag = v1.0.3
PyInstaller fallback = 1.0.2
```

### 6.2 Proposed single source of truth

**Canonical file:** `desktop/config/version.json`

Fields:

```json
{
  "name": "AICA",
  "version": "1.0.3",
  "channel": "stable",
  "build": "2026-09-01T12:00:00+05:30",
  "update": {
    "strategy": "github_releases",
    "manifest_url": "https://github.com/samrudhht2005-dev/aica-project/releases/latest/download/aica-update-manifest.json",
    "repo": "samrudhht2005-dev/aica-project"
  }
}
```

### 6.3 Build-time propagation (no manual duplication)

Enhance `build_installer.ps1` to:

1. Read `version` from `version.json` (remove hardcoded `$Version`)
2. Pass to Inno: `ISCC /DMyAppVersion={version} aica_setup.iss`
3. Set `$env:AICA_VERSION` and `$env:AICA_BUILD` for PyInstaller builds
4. Generate `pyi_rth_aica.py` default from template OR remove hardcoded version (rely on env at build time only)
5. Change `runtime_paths.py` fallback to `"0.0.0-dev"` (not a release version)

Remove `#define MyAppVersion` hardcode from `.iss`; use `{#MyAppVersion}` from compiler define only.

### 6.4 Runtime version resolution (unchanged priority)

1. `AICA_VERSION` env (set by launcher for engine child)
2. `{install_dir}/version.json` (frozen launcher)
3. `_MEIPASS/desktop/config/version.json` (engine bundle)
4. Dev project `desktop/config/version.json`

### 6.5 Release workflow (proposed)

```
Development → test → bump desktop/config/version.json only
    ↓
build_installer.ps1 (reads version.json, propagates everywhere)
    ↓
Generate SHA-256 of dist/AICA_Setup_{version}.exe
    ↓
Generate aica-update-manifest.json
    ↓
Git commit: "release: AICA v{version}"
    ↓
Git tag: v{version}
    ↓
Git push + push tag
    ↓
GitHub Release v{version}:
  - AICA_Setup_{version}.exe
  - aica-update-manifest.json
  - SHA256SUMS.txt (optional)
  - Release notes (from CHANGELOG or manifest)
    ↓
Clients detect via manifest_url
```

**Gate:** `publish_release.ps1` script fails if tag, manifest version, installer filename, and `version.json` disagree.

---

## 7. GitHub Release Workflow Design

### 7.1 Assets per release

| Asset | Required | Purpose |
|-------|----------|---------|
| `AICA_Setup_{version}.exe` | **Yes** | Inno installer — sole upgrade mechanism |
| `aica-update-manifest.json` | **Yes** | Update check + SHA-256 + metadata |
| `SHA256SUMS.txt` | Optional | Human verification |
| `AICA.exe` alone | **No** | Incomplete — missing engine bundle |
| Source zip (GitHub auto) | Informational | Not used by updater |

### 7.2 How the app finds the latest version

1. Read `update.manifest_url` from local `version.json` (shipped with install)
2. `GET manifest_url` with 5s timeout, User-Agent `AICA-Updater/{version}`
3. Parse JSON; validate schema
4. Compare semver(local, manifest.version)
5. If newer and `local >= manifest.minimum_supported_version` → offer update
6. If `local < minimum_supported_version` → treat as mandatory (or block with message)

### 7.3 Prerelease handling (future)

- Manifest field: `"channel": "stable" | "beta"`
- Local `version.json` channel must match, OR user opts into beta in settings
- Ignore GitHub prereleases unless manifest explicitly published

### 7.4 Release notes

- Short summary in manifest `release_notes` (shown in update dialog)
- Full notes in GitHub Release body (linked optionally)

---

## 8. Security Analysis

| Threat | Mitigation |
|--------|------------|
| **MITM on download** | HTTPS only; reject non-HTTPS URLs |
| **GitHub API/asset tampering** | SHA-256 verify before execute; manifest hash must match downloaded file |
| **Malicious update URL in manifest** | Allowlist hostnames: `github.com`, `objects.githubusercontent.com`; reject redirects to unknown hosts |
| **Downgrade attack** | Refuse `manifest.version < installed`; ignore older manifests |
| **Version spoofing** | Semver parse both sides; post-install verify `version.json` |
| **Path traversal in installer args** | Updater uses fixed `%LOCALAPPDATA%\AICA`; no user-supplied paths in v1 |
| **Executable replacement** | Only run installer if SHA-256 matches; installer signed path TBD |
| **Manifest substitution** | Pin `manifest_url` in shipped `version.json`; optional: embed release signing key fingerprint in v2 |
| **Rate limiting / DoS** | Check once per session + max once per 24h; 5s timeout; no retry loop on startup |
| **Privilege escalation** | `PrivilegesRequired=lowest` — per-user install only |

### Code signing (future recommendation)

- Sign `AICA.exe`, `AICA.Engine.exe`, `AICA.Updater.exe`, and `AICA_Setup_x.y.z.exe` with an Authenticode certificate
- Reduces SmartScreen warnings and strengthens integrity chain
- Not required for v1 implementation; plan hooks for signtool in build script

---

## 9. Failure and Rollback Design

| Scenario | Behavior |
|----------|----------|
| **No internet at check** | Silent skip; app continues; optional "Check for updates" in Profile |
| **Manifest fetch timeout** | Log `update_check_failed`; no UI error on startup |
| **Download fails** | Show error; keep current install; delete partial file |
| **Incomplete download** | SHA-256 fails → delete file, show error |
| **SHA-256 mismatch** | Refuse to run installer; log + alert user |
| **Installer fails (exit code ≠ 0)** | Restore from `.backup/{version}/`; alert user |
| **App crashes after update** | User can run previous backup manually; future: auto-rollback if health check fails on first launch |
| **User closes updater** | If before backup: no changes; if after backup but before install: restore or leave backup for manual recovery |
| **Old backup exists** | Keep last N=2 backups; prune older |
| **Insufficient disk space** | Pre-check: installer size × 2.5; abort before download if low |
| **Engine refuses to close** | Updater waits 30s → kill `AICA.Engine.exe` → proceed (log warning) |
| **Multiple AICA instances** | Detect all `AICA.exe` PIDs; prompt user to close other instances or force-kill after timeout |
| **Mandatory update declined** | v1: allow Later; v2: block after grace period if `mandatory: true` |

**Primary invariant:** Failed update must never leave the user without a working `%LOCALAPPDATA%\AICA\AICA.exe`.

---

## 10. User Experience Design

### 10.1 Update notification

**Recommended:** In-app banner via existing WebView2 frontend (consistent with AICA UI).

Integration point: `frontend/templates/partials/chrome.html` — dismissible banner above FAB:

```
┌──────────────────────────────────────────────────────────┐
│  ⬆ Update available — AICA v1.0.3          [Update Now] [Later] │
└──────────────────────────────────────────────────────────┘
```

Alternative/fallback: native `MessageBoxW` from launcher if WebView not ready.

### 10.2 Progress stages

| Stage | User-visible text |
|-------|-------------------|
| Checking | Checking for updates… |
| Available | Update available — AICA v1.0.3 |
| Downloading | Downloading update… {percent}% |
| Verifying | Verifying download… |
| Installing | Installing update — AICA will restart |
| Restarting | Restarting AICA… |

Progress during download/install: native updater window (tkinter or Win32) — simpler than injecting into WebView during shutdown.

### 10.3 Profile page addition

Add "Check for updates" button to `profile.html` About section — manual check without waiting for background interval.

### 10.4 pywebview bridge pattern

Extend `DesktopVoiceBridge` (`voice_bridge.py`) with:

- `check_for_updates() -> dict`
- `download_update() -> dict`
- `apply_update() -> dict`
- `get_update_status() -> dict`

Mirror existing voice API pattern used by `assistant.js`.

### 10.5 Startup performance

- Delay first check until **3 seconds after** WebView `loaded` event
- Run in `threading.Thread(daemon=True)`
- Never block `main()` or `/health` poll loop
- Cache "no update" result for 24 hours in `%LOCALAPPDATA%\AICA\webview\` localStorage or `%APPDATA%\AICA\update_check.json`

---

## 11. Implementation Phases (Overview)

| Phase | Focus |
|-------|-------|
| **1** | Version single-source-of-truth + manifest schema |
| **2** | Background update checker |
| **3** | Update UI (banner + Profile button) |
| **4** | Secure download + SHA-256 |
| **5** | `AICA.Updater.exe` |
| **6** | Rollback and recovery |
| **7** | Testing matrix |
| **8** | Release automation (`publish_release.ps1`) |

See `AUTO_UPDATER_IMPLEMENTATION_CHECKLIST.md` for step-by-step tasks.

---

## 12. Testing Plan (Summary)

Full matrix in checklist. Critical paths:

1. `1.0.2 → 1.0.3` silent upgrade preserves `aica.db`, `config.env`, personal wake profiles
2. No update → no banner
3. Offline → no crash, no banner
4. Corrupted installer → SHA fail, no install
5. User cancel → no changes
6. Engine orphan → updater kills and proceeds
7. Post-update `/health` version matches manifest
8. Desktop shortcut still launches `%LOCALAPPDATA%\AICA\AICA.exe`

---

## 13. Packaging Changes Required

| Change | Major? |
|--------|--------|
| Add `AICA.Updater.exe` to Inno `[Files]` | Small |
| Add `aica_updater.spec` PyInstaller spec | Small |
| Extend `build_installer.ps1` for version propagation | Small |
| Add manifest generation script | Small |
| Update `test_silent_install.ps1` for flattened layout | Small |
| Wire `file_version_info.txt` into specs | Optional |
| Code signing | Future |

**No major architectural change** to launcher/engine split, WebView, voice, or backend required.

---

## 14. Out of Scope (Explicit)

The following must **not** change during updater implementation:

- Click-to-talk / voice wake architecture
- POS navigation / analytics routing / organization routing
- TTS feedback / IRA auto-close lifecycle
- Async Whisper architecture
- User Roaming data layout

---

## 15. Open Decisions for Review

1. **Manifest URL:** `latest/download/aica-update-manifest.json` (rolling) vs version-pinned asset?
   - **Recommendation:** Per-release asset `aica-update-{version}.json` plus a small `aica-update-manifest.json` on `latest` that points to current stable.

2. **Updater UI:** Native Win32 vs tkinter vs console?
   - **Recommendation:** Native Win32 progress dialog (consistent with `_show_error` in launcher).

3. **Check interval:** Once per launch vs daily background?
   - **Recommendation:** Once per launch (delayed) + manual Profile check; cache 24h.

4. **Mandatory updates:** Support in manifest but defer enforcement to v1.1?
   - **Recommendation:** Yes — schema supports `mandatory`; UI treats all as optional in v1.

5. **Ship updater inside install dir vs download on demand?**
   - **Recommendation:** Ship `AICA.Updater.exe` in install dir (always available offline for apply phase).

---

*End of architecture plan.*
