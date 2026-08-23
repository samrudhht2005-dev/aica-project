# AICA Auto Updater — Implementation Checklist

**Prerequisite:** AICA v1.0.2 released on `main` with tag `v1.0.2`  
**Reference:** `AUTO_UPDATER_ARCHITECTURE_PLAN.md`  
**Rule:** Execute phases in order; do not skip rollback/testing phases.

---

## Pre-Implementation Checklist

- [ ] Read `AUTO_UPDATER_ARCHITECTURE_PLAN.md` fully
- [ ] Confirm v1.0.2 install works at `%LOCALAPPDATA%\AICA\`
- [ ] Confirm `%APPDATA%\AICA\aica.db` and `config.env` exist on test machine
- [ ] Confirm GitHub repo access for publishing test release (use prerelease for dev)
- [ ] Install Inno Setup 6 on build machine

---

## Phase 1 — Version System and Manifest Schema

**Purpose:** Eliminate version drift; define update metadata contract.

### Files to create

- [ ] `desktop/config/update_manifest.schema.json` — JSON Schema for manifest validation
- [ ] `desktop/config/update_manifest.example.json` — documented example
- [ ] `desktop/scripts/generate_update_manifest.ps1` — build manifest from version.json + installer path

### Files to modify

- [ ] `desktop/config/version.json` — add `update.manifest_url`, `update.repo`
- [ ] `desktop/scripts/build_installer.ps1`
  - Read version from `version.json` (remove hardcoded `$Version`)
  - Pass version to Inno: `ISCC /DMyAppVersion=$version`
  - Fail if `aica_setup.iss` version disagrees
- [ ] `desktop/packaging/aica_setup.iss`
  - Replace hardcoded `#define MyAppVersion` with `/D` compiler define default
  - Document in comment header
- [ ] `backend/runtime_paths.py`
  - Change `APP_VERSION` fallback to `"0.0.0-dev"` (not a release number)
- [ ] `desktop/packaging/pyi_rth_aica.py`
  - Remove hardcoded `"1.0.2"`; use build-time env injection only
- [ ] `docs/desktop/README.md` — document single version bump location

### Manifest fields (minimum)

```json
{
  "version": "1.0.3",
  "minimum_supported_version": "1.0.0",
  "channel": "stable",
  "release_notes": "...",
  "published_at": "ISO-8601",
  "download_url": "https://github.com/.../AICA_Setup_1.0.3.exe",
  "sha256": "hex-lowercase",
  "size_bytes": 0,
  "mandatory": false
}
```

### Risks

- Forgetting to update one legacy version string → **mitigate with `verify_release_version.ps1`**

### Testing

- [ ] Run build script; confirm all outputs show same version
- [ ] `GET /health` returns correct version in packaged build
- [ ] Manifest validates against schema
- [ ] SHA-256 in manifest matches installer file

---

## Phase 2 — Background Update Checker

**Purpose:** Non-blocking update discovery after startup.

### Files to create

- [ ] `desktop/launcher/update_checker.py`
  - `fetch_manifest(url, timeout=5) -> dict`
  - `parse_version(s) -> tuple` (semver)
  - `is_update_available(local, remote) -> bool`
  - `validate_manifest(m) -> bool` (schema + URL allowlist)
  - `should_check(last_check_path) -> bool` (24h cache)
- [ ] `desktop/launcher/update_config.py`
  - Manifest URL resolution from `version.json`
  - Allowed download hostnames

### Files to modify

- [ ] `desktop/launcher/main.py`
  - After WebView loaded, start daemon thread calling update checker
  - Store result in module-level or shared state for UI bridge
  - Never block startup on network
- [ ] `backend/runtime_paths.py` (optional)
  - Helper to read local install `version.json` path when frozen

### Dependencies

- Python stdlib: `urllib.request`, `json`, `threading`, `re`
- No new pip packages required for checker

### Risks

- Startup regression if check is not truly async → **gate with timeout + daemon thread**
- GitHub rate limit → **use manifest URL direct download, not API, for primary path**

### Testing

- [ ] Mock manifest newer version → checker returns update available
- [ ] Mock manifest older version → no update
- [ ] Network timeout → no exception propagates to main thread
- [ ] Invalid JSON → logged, ignored
- [ ] Cache prevents re-fetch within 24h

---

## Phase 3 — Update UI

**Purpose:** Minimal professional user-facing update experience.

### Files to create

- [ ] `frontend/static/update.js` — banner logic, progress polling
- [ ] `frontend/static/update.css` — banner styles (or add to `style.css`)

### Files to modify

- [ ] `frontend/templates/partials/chrome.html` — update banner markup (desktop-only)
- [ ] `frontend/templates/profile.html` — "Check for updates" button in About section
- [ ] `frontend/static/i18n/en.json` (+ kn, hi) — update strings
- [ ] `desktop/launcher/voice_bridge.py` — add js_api methods:
  - `check_for_updates()`
  - `get_update_status()`
  - `start_update_download()`
  - `apply_update()`
  - `dismiss_update()`
- [ ] `desktop/launcher/webview_desktop.py` — inject `window.AICA_VERSION` in bootstrap JS

### UI requirements

- [ ] Banner: "Update available — AICA v{x.y.z}"
- [ ] Buttons: **Update Now**, **Later**
- [ ] Banner hidden when no update or dismissed for session
- [ ] Profile manual check shows result inline

### Risks

- Banner shown on web (non-desktop) → **guard with `window.AICA_DESKTOP`**
- i18n missing → **add en keys first**

### Testing

- [ ] Banner appears when mock update available
- [ ] Later dismisses banner for session
- [ ] Profile button triggers check on demand
- [ ] No banner in dev web-only mode

---

## Phase 4 — Secure Download

**Purpose:** Download installer with integrity verification before any execute.

### Files to create

- [ ] `desktop/launcher/update_download.py`
  - `download_file(url, dest, progress_cb, timeout) -> Path`
  - `sha256_file(path) -> str`
  - `verify_download(path, expected_sha256, expected_size) -> bool`
  - Download dir: `%TEMP%\AICA\updates\{version}\`

### Files to modify

- [ ] `desktop/launcher/update_checker.py` — integrate download + verify flow
- [ ] `desktop/launcher/voice_bridge.py` — wire download progress to JS via evaluate_js

### Security requirements

- [ ] Reject non-HTTPS URLs
- [ ] Allowlist: `github.com`, `objects.githubusercontent.com`
- [ ] Delete partial file on hash mismatch
- [ ] Refuse downgrade (`remote_version <= local_version`)

### Risks

- Large download (~200MB) blocks UI → **download in background thread; report progress**
- Disk full → **pre-check free space >= size_bytes * 2**

### Testing

- [ ] Valid download + matching SHA → success
- [ ] Truncated file → SHA fail, file deleted
- [ ] Wrong SHA in manifest → refused
- [ ] HTTP URL → rejected

---

## Phase 5 — External Updater (`AICA.Updater.exe`)

**Purpose:** Orchestrate shutdown, install, restart — solve Windows file-lock problem.

### Files to create

- [ ] `desktop/updater/main.py` — updater entry point
- [ ] `desktop/updater/updater_win32.py` — progress UI, process wait, installer exec
- [ ] `desktop/packaging/aica_updater.spec` — PyInstaller onefile spec
- [ ] `desktop/scripts/build_updater.ps1` — build `dist/AICA.Updater.exe`

### Files to modify

- [ ] `desktop/packaging/aica_setup.iss` — add to `[Files]`:
  ```
  Source: "..\..\dist\AICA.Updater.exe"; DestDir: "{app}"; Flags: ignoreversion
  ```
- [ ] `desktop/scripts/build_installer.ps1` — call `build_updater.ps1` before Inno
- [ ] `desktop/launcher/main.py` — `apply_update()` launches updater and exits
- [ ] `desktop/launcher/update_apply.py` — launcher-side handoff logic

### Updater CLI (proposed)

```
AICA.Updater.exe
  --installer "C:\...\AICA_Setup_1.0.3.exe"
  --install-dir "%LOCALAPPDATA%\AICA"
  --wait-pid 12345
  --target-version 1.0.3
  --restart
```

### Updater steps (implement in order)

1. [ ] Parse args; validate paths exist
2. [ ] Show progress window
3. [ ] Wait for `--wait-pid` (poll 500ms, timeout 30s)
4. [ ] Wait for all `AICA.Engine.exe` processes (timeout 15s, then kill)
5. [ ] Backup install dir → `.backup\{current_version}\` (exclude `webview`, `.backup`)
6. [ ] Run installer: `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`
7. [ ] Verify `{install-dir}\version.json` version == target
8. [ ] Launch `{install-dir}\AICA.exe`
9. [ ] Exit 0

### Risks

- Updater itself locked if placed wrong → **build as separate onefile, ship adjacent to AICA.exe**
- Inno MsgBox for WebView2 → **use `/SUPPRESSMSGBOXES`; WebView2 already installed on upgraded machines**

### Testing

- [ ] Updater waits for launcher exit before installing
- [ ] Updater kills stuck engine after timeout
- [ ] Successful install relaunches AICA
- [ ] Installer exit code non-zero triggers rollback

---

## Phase 6 — Rollback and Recovery

**Purpose:** Failed update must not destroy working installation.

### Files to create

- [ ] `desktop/updater/backup.py`
  - `backup_install_dir(src, dest, exclude_dirs)`
  - `restore_backup(backup_dir, install_dir)`
  - `prune_old_backups(keep=2)`

### Files to modify

- [ ] `desktop/updater/main.py` — wrap install in try/restore
- [ ] `desktop/launcher/update_apply.py` — log failures to `%APPDATA%\AICA\logs\update.log`

### Backup location

```
%LOCALAPPDATA%\AICA\.backup\{version}\
  AICA.exe
  AICA.Engine.exe
  _internal\
  version.json
  AICA.Updater.exe
```

**Exclude:** `webview\`, `.backup\`, `logs\` (if under install dir)

### Recovery scenarios

- [ ] Installer fails → auto-restore backup
- [ ] New version fails health check on first launch (future) → prompt restore
- [ ] Document manual restore: rename `.backup\{version}` → install dir

### Testing

- [ ] Simulate installer failure (corrupt setup exe) → backup restored
- [ ] Post-rollback AICA.exe launches and `/health` returns old version
- [ ] User data (`aica.db`, config.env) unchanged after failed update

---

## Phase 7 — Testing Matrix

### Environment setup

- [ ] Machine A: clean v1.0.2 install from GitHub release
- [ ] Machine B: dev build install to `%LOCALAPPDATA%\AICA_DesktopTest`
- [ ] Publish v1.0.3 **prerelease** on GitHub for testing

### Test cases

| # | Test | Expected | Pass |
|---|------|----------|------|
| 1 | 1.0.2 → 1.0.3 update | Success; `/health` = 1.0.3 | [ ] |
| 2 | No update available | No banner; normal startup | [ ] |
| 3 | Slow internet | Download progresses; no timeout crash | [ ] |
| 4 | No internet | Silent skip; app works | [ ] |
| 5 | Interrupted download | Retry works; no partial execute | [ ] |
| 6 | Corrupted installer | SHA fail; no install | [ ] |
| 7 | SHA mismatch | Refused; alert shown | [ ] |
| 8 | User cancels (Later) | No download; app continues | [ ] |
| 9 | AppData `config.env` survives | File unchanged | [ ] |
| 10 | SQLite `aica.db` survives | Data intact; app login works | [ ] |
| 11 | Voice personal wake profiles survive | `%APPDATA%\AICA\voice\models\personal_*` present | [ ] |
| 12 | Desktop shortcut works | Opens updated AICA | [ ] |
| 13 | Backup/recovery | Failed install restores old version | [ ] |
| 14 | AICA.Engine.exe lifecycle | No orphan engine after update | [ ] |
| 15 | Update while app running | Graceful shutdown then upgrade | [ ] |
| 16 | Multiple AICA processes | Updater waits or prompts | [ ] |
| 17 | Restart after success | App opens; login/session OK | [ ] |
| 18 | WebView Remember Me | Cookie survives (webview not deleted) | [ ] |
| 19 | Mandatory manifest flag | Documented behavior (optional v1) | [ ] |
| 20 | Downgrade attempt | Refused | [ ] |

### Automated scripts to create

- [ ] `desktop/scripts/test_update_check.py` — unit tests for semver + manifest
- [ ] `desktop/scripts/test_update_e2e.ps1` — full upgrade against prerelease
- [ ] Update `desktop/scripts/test_silent_install.ps1` — fix flattened engine path

---

## Phase 8 — Release Automation

**Purpose:** Repeatable release that prevents version mismatch.

### Files to create

- [ ] `desktop/scripts/publish_release.ps1`
  - Verify git clean state (optional)
  - Run `build_installer.ps1`
  - Generate manifest + SHA-256
  - Verify all version strings match
  - Output release notes template
- [ ] `desktop/scripts/verify_release_version.ps1`
  - Scan known files for version consistency
- [ ] `.github/workflows/release.yml` (optional future)
  - Build on tag push; upload assets

### GitHub Release steps (manual until CI)

1. [ ] Bump `desktop/config/version.json` only
2. [ ] Run `publish_release.ps1`
3. [ ] Commit: `release: AICA v{x.y.z}`
4. [ ] Tag: `v{x.y.z}`
5. [ ] Push commit + tag
6. [ ] Create GitHub Release from tag
7. [ ] Upload assets:
   - [ ] `AICA_Setup_{version}.exe`
   - [ ] `aica-update-manifest.json`
   - [ ] `SHA256SUMS.txt` (optional)
8. [ ] Verify manifest URL resolves from installed v1.0.2 app (after updater shipped)

### Risks

- Publishing manifest before installer → **script generates both atomically**
- Wrong asset attached → **verify SHA in manifest matches uploaded file**

---

## File Summary — All Touch Points

### New files (expected)

```
desktop/config/update_manifest.schema.json
desktop/config/update_manifest.example.json
desktop/launcher/update_checker.py
desktop/launcher/update_config.py
desktop/launcher/update_download.py
desktop/launcher/update_apply.py
desktop/updater/main.py
desktop/updater/updater_win32.py
desktop/updater/backup.py
desktop/packaging/aica_updater.spec
desktop/scripts/build_updater.ps1
desktop/scripts/generate_update_manifest.ps1
desktop/scripts/publish_release.ps1
desktop/scripts/verify_release_version.ps1
desktop/scripts/test_update_check.py
desktop/scripts/test_update_e2e.ps1
frontend/static/update.js
frontend/static/update.css
```

### Modified files (expected)

```
desktop/config/version.json
desktop/scripts/build_installer.ps1
desktop/packaging/aica_setup.iss
desktop/packaging/pyi_rth_aica.py
desktop/launcher/main.py
desktop/launcher/voice_bridge.py
desktop/launcher/webview_desktop.py
backend/runtime_paths.py
frontend/templates/partials/chrome.html
frontend/templates/profile.html
frontend/static/i18n/en.json
frontend/static/i18n/kn.json
frontend/static/i18n/hi.json
docs/desktop/README.md
desktop/scripts/test_silent_install.ps1
```

### Must NOT modify during updater work

```
desktop/launcher/voice_wake*.py
desktop/launcher/voice_engine.py (except unrelated fixes)
desktop/launcher/voice_intents.py
frontend/static/assistant.js (voice/IRA logic)
backend/routes.py (business routes)
POS / analytics / organization routing
```

---

## Definition of Done

- [ ] v1.0.2 → v1.0.3 upgrade works on clean Windows install
- [ ] User data preserved: `config.env`, `aica.db`, voice profiles, webview session
- [ ] Failed upgrade restores previous working binaries
- [ ] Startup not noticeably slower (< 50ms impact; check is async)
- [ ] Version numbers have single source of truth
- [ ] Release script generates matching manifest + installer + tag
- [ ] Documentation updated in `docs/desktop/README.md`
- [ ] No force-update in v1 (all updates optional)

---

## Estimated Effort

| Phase | Effort |
|-------|--------|
| Phase 1 — Version + manifest | 0.5–1 day |
| Phase 2 — Background checker | 1 day |
| Phase 3 — Update UI | 1–1.5 days |
| Phase 4 — Secure download | 0.5–1 day |
| Phase 5 — External updater | 2–3 days |
| Phase 6 — Rollback | 1 day |
| Phase 7 — Testing | 2 days |
| Phase 8 — Release automation | 0.5–1 day |
| **Total** | **~9–12 days** |

---

## First Implementation Step (When Approved)

**Start with Phase 1:**

1. Add `update.manifest_url` to `desktop/config/version.json`
2. Refactor `build_installer.ps1` to read version from `version.json` only
3. Create `verify_release_version.ps1` to catch drift
4. Create `generate_update_manifest.ps1` + example manifest

This unblocks all later phases and reduces release risk before any runtime update code ships.

---

*Checklist ends — do not implement until plan is approved.*
