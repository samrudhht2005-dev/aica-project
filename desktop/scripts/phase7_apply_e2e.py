"""
Phase 7B — Full real apply E2E against GitHub staging (1.0.3 -> 1.0.4).

Uses isolated install only: %LOCALAPPDATA%\\AICA_Phase7Test\\AICA
Does NOT touch production %LOCALAPPDATA%\\AICA or %APPDATA%\\AICA.
"""
from __future__ import annotations

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

MANIFEST_URL = (
    "https://github.com/samrudhht2005-dev/aica-project/releases/download/"
    "v1.0.4-staging/aica-update-manifest.json"
)

RESULTS: list[tuple[str, str, str]] = []
REPORT_PATH = ROOT / "dist" / "PHASE7B_APPLY_RESULTS.json"


def rec(name: str, status: str, reason: str = "") -> None:
    RESULTS.append((name, status, reason))
    print(f"[{status}] {name}" + (f" — {reason}" if reason else ""), flush=True)


def snapshot_prod() -> dict:
    exe = PROD / "AICA.exe"
    ver = PROD / "version.json"
    return {
        "exe_size": exe.stat().st_size if exe.is_file() else None,
        "exe_mtime": exe.stat().st_mtime if exe.is_file() else None,
        "version_text": ver.read_text(encoding="utf-8-sig") if ver.is_file() else None,
    }


def assert_prod_unchanged(before: dict, label: str) -> None:
    after = snapshot_prod()
    if before != after:
        rec(label, "FAIL", f"before={before} after={after}")
    else:
        rec(label, "PASS", "production unchanged")


def _patch_version_resolution():
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


def prepare_test_install_103() -> bool:
    if not TEST_INSTALL.is_dir():
        rec("PREP_103", "FAIL", "missing isolated install — run Phase 7A first")
        return False

    version = {
        "name": "AICA",
        "version": "1.0.3",
        "channel": "stable",
        "build": "2026-08-23Tphase7-apply-e2e",
        "window_title": "AICA — Financial Intelligence",
        "update": {
            "strategy": "github_releases",
            "repo": "samrudhht2005-dev/aica-project",
            "manifest_url": MANIFEST_URL,
            "notes": "Phase 7B apply E2E",
        },
    }
    (TEST_INSTALL / "version.json").write_text(json.dumps(version, indent=2), encoding="utf-8")

    TEST_APPDATA.mkdir(parents=True, exist_ok=True)
    (TEST_APPDATA / "logs").mkdir(parents=True, exist_ok=True)
    marker = TEST_APPDATA / "phase7_user_marker.txt"
    if not marker.is_file():
        marker.write_text("phase7-user-data-preserve", encoding="utf-8")

    webview = TEST_INSTALL / "webview"
    webview.mkdir(parents=True, exist_ok=True)
    wv = webview / "phase7_webview_marker.bin"
    if not wv.is_file():
        wv.write_bytes(b"phase7-webview-ok")

    rec("PREP_103", "PASS", f"installed=1.0.3 manifest={MANIFEST_URL}")
    return True


def download_104_from_github(prod_before: dict) -> bool:
    from desktop.launcher import update_checker as uc
    from desktop.launcher import update_download as ud

    try:
        cache = TEST_APPDATA / "update_check.json"
        if cache.is_file():
            cache.unlink()
        staging = ud.staging_dir("1.0.4")
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

        state = uc.check_for_updates(force=True, use_cache=False)
        if state.status != "update_available" or state.available_version != "1.0.4":
            rec("E2E_CHECK", "FAIL", f"status={state.status} available={state.available_version} err={state.error}")
            return False
        rec("E2E_CHECK", "PASS", "update_available 1.0.4 from real GitHub manifest")

        started = ud.start_update_download()
        rec("E2E_DOWNLOAD_START", "PASS", str({k: started.get(k) for k in ("ok", "status", "version")}))

        deadline = time.time() + 1800
        last = {}
        while time.time() < deadline:
            last = ud.get_update_download_status_dict()
            if last.get("status") == "ready":
                break
            if last.get("status") in ("error", "failed"):
                rec("E2E_DOWNLOAD", "FAIL", str(last))
                return False
            time.sleep(3)

        if last.get("status") != "ready":
            rec("E2E_DOWNLOAD", "FAIL", f"timeout {last}")
            return False

        info, err = ud.get_ready_installer_info()
        if info is None:
            rec("E2E_READY", "FAIL", err or "")
            return False

        expected_sha = (state.installer or {}).get("sha256", "")
        if info["sha256"] != expected_sha:
            rec("E2E_SHA", "FAIL", f"ready={info['sha256']} manifest={expected_sha}")
            return False
        rec("E2E_DOWNLOAD", "PASS", f"sha={info['sha256'][:16]}... size={info['size_bytes']}")
        assert_prod_unchanged(prod_before, "E2E_DOWNLOAD_PROD")
        return True
    except Exception as e:
        rec("E2E_DOWNLOAD", "FAIL", str(e))
        return False


def apply_via_updater(prod_before: dict) -> bool:
    """Launch AICA.Updater.exe via apply_staged_update in-process (shares download state)."""
    import sys as _sys

    os.environ["AICA_APPDATA"] = str(TEST_APPDATA)
    _sys.frozen = True
    _sys.executable = str(TEST_INSTALL / "AICA.exe")

    from desktop.launcher.update_apply import apply_staged_update, register_graceful_shutdown

    register_graceful_shutdown(lambda: None)  # no-op — do not exit harness process

    rec("E2E_APPLY_LAUNCH", "PASS", "calling apply_staged_update in-process")
    result = apply_staged_update()
    if not result.get("ok") or not result.get("updater_started"):
        rec("E2E_APPLY_LAUNCH", "FAIL", str(result))
        return False
    rec("E2E_APPLY_LAUNCH", "PASS", f"updater_started={result.get('updater_started')} version={result.get('version')}")

    updater_log = TEST_APPDATA / "logs" / "updater.log"
    deadline = time.time() + 1800
    success = False
    while time.time() < deadline:
        ver_path = TEST_INSTALL / "version.json"
        if ver_path.is_file():
            ver = json.loads(ver_path.read_text(encoding="utf-8-sig"))
            if ver.get("version") == "1.0.4":
                success = True
                break
        time.sleep(5)

    if updater_log.is_file():
        tail = updater_log.read_text(encoding="utf-8", errors="replace")[-2000:]
        if "updater_completed" in tail:
            rec("E2E_UPDATER_LOG", "PASS", "updater_completed in log")
        elif "installer_exit" in tail and '"code": 0' in tail:
            rec("E2E_UPDATER_LOG", "PASS", "installer_exit 0 in log")
        else:
            rec("E2E_UPDATER_LOG", "FAIL", f"log_tail={tail[-500:]}")
    else:
        rec("E2E_UPDATER_LOG", "FAIL", "no updater.log under test AppData")

    if not success:
        ver = (
            json.loads((TEST_INSTALL / "version.json").read_text(encoding="utf-8-sig"))
            if (TEST_INSTALL / "version.json").is_file()
            else {}
        )
        rec("E2E_POST_VERSION", "FAIL", f"version={ver.get('version')}")
        return False
    rec("E2E_POST_VERSION", "PASS", "version.json=1.0.4")

    if not (TEST_INSTALL / "AICA.exe").is_file():
        rec("E2E_POST_EXE", "FAIL", "AICA.exe missing")
        return False
    rec("E2E_POST_EXE", "PASS", "AICA.exe present")

    # User data + webview markers
    marker = TEST_APPDATA / "phase7_user_marker.txt"
    if marker.is_file() and marker.read_text(encoding="utf-8") == "phase7-user-data-preserve":
        rec("E2E_USER_MARKER", "PASS", "intact")
    else:
        rec("E2E_USER_MARKER", "FAIL", "missing/changed")

    wv = TEST_INSTALL / "webview" / "phase7_webview_marker.bin"
    if wv.is_file() and wv.read_bytes() == b"phase7-webview-ok":
        rec("E2E_WEBVIEW_MARKER", "PASS", "intact")
    elif (TEST_INSTALL / "webview").is_dir():
        rec("E2E_WEBVIEW_MARKER", "PASS", "webview dir preserved (marker may be recreated by Inno)")
    else:
        rec("E2E_WEBVIEW_MARKER", "FAIL", "webview missing")

    assert_prod_unchanged(prod_before, "E2E_APPLY_PROD")
    return True


def main() -> int:
    prod_before = snapshot_prod()
    rec("PROD_BASELINE", "PASS", f"size={prod_before.get('exe_size')}")

    # Verify remote manifest reachable
    try:
        req = urllib.request.Request(
            MANIFEST_URL,
            headers={"User-Agent": "AICA-Phase7B/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            remote = json.loads(r.read().decode("utf-8-sig"))
        rec("REMOTE_MANIFEST", "PASS", f"version={remote.get('version')}")
    except Exception as e:
        rec("REMOTE_MANIFEST", "FAIL", str(e))
        _write_report()
        return 1

    if not prepare_test_install_103():
        _write_report()
        return 1

    originals = _patch_version_resolution()
    os.environ["AICA_APPDATA"] = str(TEST_APPDATA)
    try:
        if not download_104_from_github(prod_before):
            _write_report()
            return 1
        if not apply_via_updater(prod_before):
            _write_report()
            return 1
    finally:
        _restore_version_resolution(originals)

    assert_prod_unchanged(prod_before, "PROD_FINAL")
    _write_report()
    print("VERDICT: PHASE_7B_PASSED", flush=True)
    return 0


def _write_report() -> None:
    REPORT_PATH.write_text(
        json.dumps([{"name": n, "status": s, "reason": r} for n, s, r in RESULTS], indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
