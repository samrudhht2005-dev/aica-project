"""
Phase 7A — Real GitHub staging validation (Tests A–D).

Does NOT touch %LOCALAPPDATA%\\AICA production.
Isolated install: %LOCALAPPDATA%\\AICA_Phase7Test\\AICA
Test AppData:     %LOCALAPPDATA%\\AICA_Phase7TestAppData
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOCAL = Path(os.environ["LOCALAPPDATA"])
PROD = LOCAL / "AICA"
TEST_ROOT = LOCAL / "AICA_Phase7Test"
TEST_INSTALL = TEST_ROOT / "AICA"
TEST_APPDATA = LOCAL / "AICA_Phase7TestAppData"
TEMP = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")

MANIFEST_URL = (
    "https://github.com/samrudhht2005-dev/aica-project/releases/download/"
    "v1.0.3-staging/aica-update-manifest.json"
)
BAD_MANIFEST_URL = (
    "https://github.com/samrudhht2005-dev/aica-project/releases/download/"
    "v1.0.3-staging/aica-update-manifest-bad-hash.json"
)
INSTALLER_URL = (
    "https://github.com/samrudhht2005-dev/aica-project/releases/download/"
    "v1.0.3-staging/AICA_Setup_1.0.3.exe"
)

RESULTS: list[tuple[str, str, str]] = []
REPORT_PATH = ROOT / "dist" / "PHASE7A_STAGING_RESULTS.json"


def rec(name: str, status: str, reason: str = "") -> None:
    RESULTS.append((name, status, reason))
    print(f"[{status}] {name}" + (f" — {reason}" if reason else ""), flush=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def snapshot_prod() -> dict:
    exe = PROD / "AICA.exe"
    ver = PROD / "version.json"
    return {
        "exe_exists": exe.is_file(),
        "exe_size": exe.stat().st_size if exe.is_file() else None,
        "exe_mtime": exe.stat().st_mtime if exe.is_file() else None,
        "version_text": ver.read_text(encoding="utf-8-sig") if ver.is_file() else None,
    }


def assert_prod_unchanged(before: dict, label: str) -> None:
    after = snapshot_prod()
    if before != after:
        rec(label, "FAIL", f"before={before} after={after}")
    else:
        rec(label, "PASS", "production %LOCALAPPDATA%\\AICA unchanged")


def _kill_test_install_processes() -> None:
    target = str(TEST_INSTALL.resolve()).lower()
    try:
        import psutil
    except Exception:
        psutil = None
    if psutil is not None:
        for proc in psutil.process_iter(["pid", "exe"]):
            try:
                exe = (proc.info.get("exe") or "").lower()
                if exe and target in exe:
                    proc.kill()
            except Exception:
                pass
        return
    ps = (
        f"Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.ExecutablePath -and $_.ExecutablePath.ToLower().StartsWith('{target}'.Replace('\\','\\\\')) }} | "
        f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=30, check=False)


def setup_isolated_install() -> bool:
    """Copy 1.0.3 updater-enabled binaries; report installed version as 1.0.2 for update detection."""
    _kill_test_install_processes()
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT, ignore_errors=True)
    if TEST_APPDATA.exists():
        shutil.rmtree(TEST_APPDATA, ignore_errors=True)

    TEST_INSTALL.mkdir(parents=True, exist_ok=True)
    TEST_APPDATA.mkdir(parents=True, exist_ok=True)
    (TEST_APPDATA / "logs").mkdir(parents=True, exist_ok=True)
    (TEST_APPDATA / "phase7_user_marker.txt").write_text("phase7-user-data-preserve", encoding="utf-8")

    src_exe = ROOT / "dist" / "AICA.exe"
    src_upd = ROOT / "dist" / "AICA.Updater.exe"
    src_eng = ROOT / "dist" / "AICA.Engine"
    if not src_exe.is_file() or not src_upd.is_file() or not (src_eng / "AICA.Engine.exe").is_file():
        rec("ISOLATED_SETUP", "FAIL", "missing dist AICA.exe / Updater / Engine")
        return False

    shutil.copy2(src_exe, TEST_INSTALL / "AICA.exe")
    shutil.copy2(src_upd, TEST_INSTALL / "AICA.Updater.exe")
    shutil.copy2(src_eng / "AICA.Engine.exe", TEST_INSTALL / "AICA.Engine.exe")
    shutil.copytree(src_eng / "_internal", TEST_INSTALL / "_internal", dirs_exist_ok=True)
    for name in ("config.env.example", "README_CONFIG.txt"):
        src = ROOT / "desktop" / "config" / name
        if src.is_file():
            shutil.copy2(src, TEST_INSTALL / name)

    webview = TEST_INSTALL / "webview"
    webview.mkdir(parents=True, exist_ok=True)
    (webview / "phase7_webview_marker.bin").write_bytes(b"phase7-webview-ok")

    # Report as 1.0.2 so staging 1.0.3 is detected as an update (updater binaries are 1.0.3).
    version = {
        "name": "AICA",
        "version": "1.0.2",
        "channel": "stable",
        "build": "2026-08-23Tphase7-isolated",
        "window_title": "AICA — Financial Intelligence",
        "update": {
            "strategy": "github_releases",
            "repo": "samrudhht2005-dev/aica-project",
            "manifest_url": MANIFEST_URL,
            "notes": "Phase 7 isolated staging test install",
        },
    }
    (TEST_INSTALL / "version.json").write_text(json.dumps(version, indent=2), encoding="utf-8")
    rec(
        "ISOLATED_SETUP",
        "PASS",
        f"install={TEST_INSTALL} appdata={TEST_APPDATA} reported=1.0.2 manifest={MANIFEST_URL}",
    )
    return True


def write_install_version(*, version: str, manifest_url: str) -> None:
    data = json.loads((TEST_INSTALL / "version.json").read_text(encoding="utf-8-sig"))
    data["version"] = version
    data["update"]["manifest_url"] = manifest_url
    (TEST_INSTALL / "version.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def https_get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "AICA-Phase7-Staging/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
        if not url.lower().startswith("https://"):
            raise RuntimeError("non-https")
        return json.loads(raw.decode("utf-8-sig"))


def test_a_manifest_fetch() -> dict | None:
    print("\n=== TEST A — Real GitHub manifest fetch ===", flush=True)
    try:
        data = https_get_json(MANIFEST_URL)
        rec("TEST_A_fetch", "PASS", f"version={data.get('version')} channel={data.get('channel')}")
    except Exception as e:
        rec("TEST_A_fetch", "FAIL", str(e))
        return None

    from desktop.launcher.update_checker import validate_manifest

    validated, err = validate_manifest(data, installed_version="1.0.2")
    if validated is None:
        rec("TEST_A_validate", "FAIL", err or "unknown")
        return None
    rec(
        "TEST_A_validate",
        "PASS",
        f"version={validated['version']} sha={validated['installer']['sha256'][:16]}... "
        f"size={validated['installer']['size_bytes']} url={validated['installer']['url']}",
    )
    if not validated["installer"]["url"].startswith("https://github.com/"):
        rec("TEST_A_https", "FAIL", validated["installer"]["url"])
    else:
        rec("TEST_A_https", "PASS", validated["installer"]["url"])
    return validated


def test_b_update_ui(prod_before: dict) -> bool:
    print("\n=== TEST B — Update UI / status via isolated AICA.exe ===", flush=True)
    _kill_test_install_processes()
    # Clear prior update log / cache under test AppData
    log_path = TEST_APPDATA / "logs" / "update.log"
    if log_path.is_file():
        log_path.unlink()
    cache = TEST_APPDATA / "update_cache.json"
    if cache.is_file():
        cache.unlink()

    env = os.environ.copy()
    env["AICA_APPDATA"] = str(TEST_APPDATA)
    # Keep WebView/profile under test AppData only
    env.pop("AICA_ROOT", None)

    exe = TEST_INSTALL / "AICA.exe"
    proc = subprocess.Popen(
        [str(exe)],
        cwd=str(TEST_INSTALL),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    rec("TEST_B_launch", "PASS", f"pid={proc.pid}")

    deadline = time.time() + 90
    found_available = False
    found_fetch = False
    last_log = ""
    while time.time() < deadline:
        if log_path.is_file():
            last_log = log_path.read_text(encoding="utf-8", errors="replace")
            if "update_manifest_fetched" in last_log:
                found_fetch = True
            if "update_available" in last_log:
                found_available = True
                break
            if "update_manifest_rejected" in last_log or "update_check_fetch_failed" in last_log:
                break
        time.sleep(1.5)

    # Also probe health if engine is up
    health_ok = False
    health_version = None
    try:
        import urllib.error

        for port in (8765, 8000, 5000, 5173):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    body = r.read().decode("utf-8", errors="replace")
                    health_ok = True
                    try:
                        health_version = json.loads(body).get("version")
                    except Exception:
                        health_version = body[:120]
                    break
            except Exception:
                continue
    except Exception:
        pass

    if found_available:
        rec("TEST_B_status", "PASS", "update.log shows update_available")
    elif found_fetch:
        rec("TEST_B_status", "FAIL", f"manifest fetched but no update_available; log_tail={last_log[-500:]}")
    else:
        rec("TEST_B_status", "FAIL", f"no update_available within timeout; log_tail={last_log[-500:]}")

    # Banner assets present in packaged frontend
    update_js = TEST_INSTALL / "_internal" / "frontend" / "static" / "update.js"
    update_css = TEST_INSTALL / "_internal" / "frontend" / "static" / "update.css"
    if update_js.is_file() and update_css.is_file():
        rec("TEST_B_ui_assets", "PASS", "update.js + update.css present in isolated install")
    else:
        rec("TEST_B_ui_assets", "FAIL", f"js={update_js.is_file()} css={update_css.is_file()}")

    if health_ok:
        rec("TEST_B_health", "PASS", f"health responded version={health_version}")
    else:
        rec("TEST_B_health", "PASS", "health probe optional — status driven by update.log")

    _kill_test_install_processes()
    time.sleep(1)
    assert_prod_unchanged(prod_before, "TEST_B_prod_preserved")
    return found_available


def _patch_version_resolution():
    """Make Python harness read isolated install version.json (not repo 1.0.3)."""
    from desktop.launcher import update_config as cfg
    import backend.runtime_paths as rp

    original_candidates = cfg._version_json_candidates
    original_release_info = rp.app_release_info

    def _candidates() -> list[Path]:
        return [TEST_INSTALL / "version.json"]

    def _release_info() -> dict:
        data = json.loads((TEST_INSTALL / "version.json").read_text(encoding="utf-8-sig"))
        return {
            "name": "AICA",
            "version": str(data.get("version") or ""),
            "build": str(data.get("build") or ""),
            "channel": str(data.get("channel") or "stable"),
        }

    cfg._version_json_candidates = _candidates  # type: ignore
    rp.app_release_info = _release_info  # type: ignore
    return original_candidates, original_release_info


def _restore_version_resolution(originals) -> None:
    from desktop.launcher import update_config as cfg
    import backend.runtime_paths as rp

    cfg._version_json_candidates = originals[0]  # type: ignore
    rp.app_release_info = originals[1]  # type: ignore


def test_c_real_download(validated: dict, prod_before: dict) -> Path | None:
    print("\n=== TEST C — Real installer download from GitHub ===", flush=True)
    os.environ["AICA_APPDATA"] = str(TEST_APPDATA)
    write_install_version(version="1.0.2", manifest_url=MANIFEST_URL)

    from desktop.launcher import update_checker as uc
    from desktop.launcher import update_download as ud

    originals = _patch_version_resolution()
    try:
        # Clear any prior staging for 1.0.3
        staging = ud.staging_dir("1.0.3")
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

        cache = TEST_APPDATA / "update_check.json"
        if cache.is_file():
            cache.unlink()

        state = uc.check_for_updates(force=True, use_cache=False)
        if state.status != "update_available":
            rec("TEST_C_precheck", "FAIL", f"status={state.status} error={state.error}")
            return None
        rec("TEST_C_precheck", "PASS", f"available={state.available_version}")

        started = ud.start_update_download()
        if not started.get("ok") and not started.get("already_in_progress") and not started.get("already_ready"):
            if started.get("status") not in ("downloading", "verifying", "ready", "starting"):
                rec("TEST_C_start", "FAIL", str(started))
                return None
        rec("TEST_C_start", "PASS", str({k: started.get(k) for k in ("ok", "status", "version")}))

        deadline = time.time() + 1800
        last = {}
        last_print = 0
        while time.time() < deadline:
            last = ud.get_update_download_status_dict()
            st = last.get("status")
            if st == "ready":
                break
            if st in ("error", "failed", "hash_mismatch", "sha_mismatch"):
                rec("TEST_C_download", "FAIL", str(last))
                return None
            now = int(time.time())
            if now - last_print >= 15:
                print(f"  download status={st} progress={last.get('progress_percent')} bytes={last.get('bytes_downloaded')}", flush=True)
                last_print = now
            time.sleep(2)

        if last.get("status") != "ready":
            rec("TEST_C_download", "FAIL", f"timeout last={last}")
            return None

        info, err = ud.get_ready_installer_info()
        if info is None:
            rec("TEST_C_ready", "FAIL", err or "no ready info")
            return None

        path = Path(info["installer_path"])
        size = path.stat().st_size
        digest = sha256_file(path)
        expected_sha = validated["installer"]["sha256"]
        expected_size = validated["installer"]["size_bytes"]

        if size != expected_size:
            rec("TEST_C_size", "FAIL", f"got={size} expected={expected_size}")
            return None
        rec("TEST_C_size", "PASS", str(size))

        if digest != expected_sha:
            rec("TEST_C_sha256", "FAIL", f"got={digest} expected={expected_sha}")
            return None
        rec("TEST_C_sha256", "PASS", digest)
        rec("TEST_C_download", "PASS", f"path={path}")
        assert_prod_unchanged(prod_before, "TEST_C_prod_preserved")
        return path
    finally:
        _restore_version_resolution(originals)


def test_d_hash_failure(prod_before: dict) -> bool:
    print("\n=== TEST D — Hash failure against staging bad-hash manifest ===", flush=True)
    os.environ["AICA_APPDATA"] = str(TEST_APPDATA)

    from desktop.launcher import update_checker as uc
    from desktop.launcher import update_download as ud

    write_install_version(version="1.0.2", manifest_url=BAD_MANIFEST_URL)
    originals = _patch_version_resolution()
    try:
        staging = ud.staging_dir("1.0.3")
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

        cache = TEST_APPDATA / "update_check.json"
        if cache.is_file():
            cache.unlink()

        state = uc.check_for_updates(force=True, use_cache=False)
        if state.status != "update_available":
            rec("TEST_D_precheck", "FAIL", f"status={state.status} error={state.error}")
            return False
        bad_sha = (state.installer or {}).get("sha256", "")
        if bad_sha == "a4ba527f63a809fa7e784402bad392897da42f256fb97f6d20db54a41e138320":
            rec("TEST_D_precheck", "FAIL", "manifest still has correct hash — bad-hash asset not used")
            return False
        rec("TEST_D_precheck", "PASS", f"bad_sha={bad_sha}")

        started = ud.start_update_download()
        rec("TEST_D_start", "PASS", str({k: started.get(k) for k in ("ok", "status")}))

        deadline = time.time() + 1800
        last = {}
        while time.time() < deadline:
            last = ud.get_update_download_status_dict()
            st = str(last.get("status") or "")
            if st in ("error", "failed", "hash_mismatch", "sha_mismatch", "verification_failed"):
                break
            if st == "ready":
                rec("TEST_D_reject", "FAIL", "download reached ready with bad hash")
                return False
            time.sleep(2)

        st = str(last.get("status") or "")
        err = str(last.get("error") or last.get("code") or "")
        if st == "ready":
            rec("TEST_D_reject", "FAIL", "ready despite bad hash")
            return False
        # Require a verification/hash failure after a real download attempt — not a
        # generic early download crash (those are Test C regressions).
        log_path = TEST_APPDATA / "logs" / "update.log"
        log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:] if log_path.is_file() else ""
        hash_logged = "update_download_sha_mismatch" in log_tail or "sha_mismatch" in log_tail
        user_verify_msg = "unable to verify" in err.lower()
        if hash_logged or user_verify_msg:
            rec("TEST_D_reject", "PASS", f"status={st} error={err} hash_logged={hash_logged}")
        else:
            rec(
                "TEST_D_reject",
                "FAIL",
                f"not a hash-verify failure (status={st} error={err}); log_tail={log_tail[-400:]}",
            )
            return False

        info, _ = ud.get_ready_installer_info()
        if info is not None:
            rec("TEST_D_no_ready", "FAIL", "ready installer info present")
            return False
        rec("TEST_D_no_ready", "PASS", "no ready installer after hash failure")
        assert_prod_unchanged(prod_before, "TEST_D_prod_preserved")

        # Restore good 1.0.3 install state for Phase 7B
        write_install_version(version="1.0.3", manifest_url=MANIFEST_URL)
        return True
    finally:
        _restore_version_resolution(originals)


def main() -> int:
    prod_before = snapshot_prod()
    rec("PROD_BASELINE", "PASS", f"size={prod_before.get('exe_size')}")

    if not setup_isolated_install():
        _write_report()
        return 1

    validated = test_a_manifest_fetch()
    if validated is None:
        assert_prod_unchanged(prod_before, "PROD_FINAL")
        _write_report()
        return 1

    ui_ok = test_b_update_ui(prod_before)
    path = test_c_real_download(validated, prod_before)
    d_ok = test_d_hash_failure(prod_before)

    # Markers
    marker = TEST_APPDATA / "phase7_user_marker.txt"
    if marker.is_file() and marker.read_text(encoding="utf-8") == "phase7-user-data-preserve":
        rec("USER_MARKER", "PASS", "intact")
    else:
        rec("USER_MARKER", "FAIL", "missing/changed")

    wv = TEST_INSTALL / "webview" / "phase7_webview_marker.bin"
    if wv.is_file() and wv.read_bytes() == b"phase7-webview-ok":
        rec("WEBVIEW_MARKER", "PASS", "intact")
    else:
        rec("WEBVIEW_MARKER", "FAIL", "missing/changed")

    assert_prod_unchanged(prod_before, "PROD_FINAL")
    _write_report()

    fails = [r for r in RESULTS if r[1] == "FAIL"]
    print("\n=== Phase 7A summary ===", flush=True)
    for name, status, reason in RESULTS:
        print(f"  {status:4} {name} {reason}", flush=True)
    if fails or validated is None or path is None or not ui_ok or not d_ok:
        print("VERDICT: PHASE_7A_FAILED", flush=True)
        return 1
    print("VERDICT: PHASE_7A_PASSED", flush=True)
    return 0


def _write_report() -> None:
    REPORT_PATH.write_text(
        json.dumps([{"name": n, "status": s, "reason": r} for n, s, r in RESULTS], indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {REPORT_PATH}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
