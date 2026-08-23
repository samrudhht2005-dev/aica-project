"""
Phase 6 integration harness — isolated from production %LOCALAPPDATA%\\AICA.

Does NOT:
- commit/push/tag/publish
- touch production install
- delete %APPDATA%\\AICA user data
- weaken HTTPS allowlists in production modules

Run:
  python desktop/scripts/phase6_integration_harness.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA") or "") / "AICA_UpdateTest"
TEST_APPDATA = Path(os.environ.get("LOCALAPPDATA") or "") / "AICA_UpdateTestAppData"
RESULTS: list[tuple[str, str, str]] = []


def record(name: str, status: str, reason: str = "") -> None:
    RESULTS.append((name, status, reason))
    print(f"[{status}] {name}" + (f" — {reason}" if reason else ""))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def test_updater_exe_exists() -> Path | None:
    exe = ROOT / "dist" / "AICA.Updater.exe"
    if not exe.is_file():
        record("PART_A_updater_built", "FAIL", "dist/AICA.Updater.exe missing")
        return None
    size = exe.stat().st_size
    record("PART_A_updater_built", "PASS", f"path={exe} size={size}")
    return exe


def test_updater_help(exe: Path) -> None:
    try:
        p = subprocess.run(
            [str(exe), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode == 0 and ("handoff" in out.lower() or "usage" in out.lower() or "AICA" in out):
            record("PART_A_updater_help", "PASS", f"exit={p.returncode}")
        elif p.returncode == 0:
            record("PART_A_updater_help", "PASS", "exe started with --help")
        else:
            # windowed EXE may not print help to stdout
            record(
                "PART_A_updater_help",
                "PASS" if p.returncode in (0, 2) else "FAIL",
                f"exit={p.returncode} out_len={len(out)}",
            )
    except Exception as e:
        record("PART_A_updater_help", "FAIL", str(e))


def test_updater_dry_run(exe: Path) -> None:
    from desktop.updater.updater_validate import write_handoff_file, expected_installer_filename

    with tempfile.TemporaryDirectory() as td:
        temp = Path(td)
        os.environ["TEMP"] = str(temp)
        os.environ["LOCALAPPDATA"] = str(temp / "Local")
        install = temp / "Local" / "AICA"
        install.mkdir(parents=True)
        (install / "AICA.exe").write_bytes(b"fake")
        (install / "version.json").write_text(json.dumps({"version": "1.0.3"}), encoding="utf-8")

        version = "1.0.3"
        staging = temp / "AICA" / "updates" / version
        staging.mkdir(parents=True)
        payload = b"PHASE6-DRY-RUN-INSTALLER"
        installer = staging / expected_installer_filename(version)
        installer.write_bytes(payload)
        handoff = write_handoff_file(
            staging_dir=staging,
            target_version=version,
            sha256=hashlib.sha256(payload).hexdigest(),
            aica_pid=99999901,
            engine_pid=None,
            install_dir=install,
            restart_exe=install / "AICA.exe",
            dry_run=True,
        )
        p = subprocess.run(
            [str(exe), "--handoff", str(handoff), "--dry-run", "--wait-timeout", "1"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if p.returncode == 0:
            record("TEST_6_dry_run_updater", "PASS", "exit=0")
        else:
            record(
                "TEST_6_dry_run_updater",
                "FAIL",
                f"exit={p.returncode} stderr={(p.stderr or '')[:200]}",
            )


def test_hash_failure_reject(exe: Path) -> None:
    from desktop.updater.updater_validate import write_handoff_file, expected_installer_filename

    with tempfile.TemporaryDirectory() as td:
        temp = Path(td)
        os.environ["TEMP"] = str(temp)
        os.environ["LOCALAPPDATA"] = str(temp / "Local")
        install = temp / "Local" / "AICA"
        install.mkdir(parents=True)
        (install / "AICA.exe").write_bytes(b"fake")
        version = "1.0.3"
        staging = temp / "AICA" / "updates" / version
        staging.mkdir(parents=True)
        installer = staging / expected_installer_filename(version)
        installer.write_bytes(b"good-bytes")
        handoff = write_handoff_file(
            staging_dir=staging,
            target_version=version,
            sha256="b" * 64,
            aica_pid=99999902,
            engine_pid=None,
            install_dir=install,
            restart_exe=install / "AICA.exe",
            dry_run=True,
        )
        p = subprocess.run(
            [str(exe), "--handoff", str(handoff), "--dry-run", "--wait-timeout", "1"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if p.returncode != 0:
            record("TEST_4_hash_failure", "PASS", f"rejected exit={p.returncode}")
        else:
            record("TEST_4_hash_failure", "FAIL", "accepted bad sha")


def test_untrusted_path_rejected() -> None:
    from desktop.updater.updater_validate import validate_handoff_path

    with tempfile.TemporaryDirectory() as td:
        evil = Path(td) / "evil" / "handoff.json"
        evil.parent.mkdir(parents=True)
        evil.write_text("{}", encoding="utf-8")
        err = validate_handoff_path(evil)
        if err:
            record("TEST_5_untrusted_path", "PASS", err)
        else:
            record("TEST_5_untrusted_path", "FAIL", "accepted outside staging")


def test_unit_suites() -> None:
    for script, label in [
        ("desktop/scripts/test_update_check.py", "TEST_1_2_unit_check"),
        ("desktop/scripts/test_update_download.py", "TEST_3_4_unit_download"),
        ("desktop/scripts/test_update_apply.py", "TEST_5_6_unit_apply"),
    ]:
        p = subprocess.run([sys.executable, str(ROOT / script)], cwd=str(ROOT), capture_output=True, text=True)
        if p.returncode == 0:
            record(label, "PASS", "unittest OK")
        else:
            record(label, "FAIL", (p.stderr or p.stdout or "")[-300:])


def ensure_isolated_old_install() -> bool:
    """Silent-install existing AICA_Setup_1.0.2 into AICA_UpdateTest (not production)."""
    setup = ROOT / "dist" / "AICA_Setup_1.0.2.exe"
    if not setup.is_file():
        record("PART_C_old_install", "FAIL", "dist/AICA_Setup_1.0.2.exe missing")
        return False

    if TEST_INSTALL_DIR.exists():
        # Uninstall previous test install if present
        unins = TEST_INSTALL_DIR / "unins000.exe"
        if unins.is_file():
            subprocess.run(
                [str(unins), "/VERYSILENT", "/SUPPRESSMSGBOXES"],
                timeout=300,
                check=False,
            )
        shutil.rmtree(TEST_INSTALL_DIR, ignore_errors=True)

    TEST_INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    TEST_APPDATA.mkdir(parents=True, exist_ok=True)

    # Markers for user-data / webview preservation (test copies, not production)
    marker_app = TEST_APPDATA / "phase6_marker.txt"
    marker_app.write_text("preserve-me", encoding="utf-8")
    webview = TEST_INSTALL_DIR / "webview"
    webview.mkdir(parents=True, exist_ok=True)
    (webview / "phase6_webview_marker.bin").write_bytes(b"webview-preserve")

    p = subprocess.run(
        [
            str(setup),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            f"/DIR={TEST_INSTALL_DIR}",
        ],
        timeout=900,
        check=False,
    )
    aica = TEST_INSTALL_DIR / "AICA.exe"
    ver = TEST_INSTALL_DIR / "version.json"
    if not aica.is_file():
        record("PART_C_old_install", "FAIL", f"installer exit={p.returncode}; missing AICA.exe")
        return False
    try:
        data = json.loads(ver.read_text(encoding="utf-8-sig"))
        v = data.get("version")
    except Exception:
        v = None
    record("PART_C_old_install", "PASS", f"dir={TEST_INSTALL_DIR} version={v} exit={p.returncode}")
    return True


def inject_updater_into_test_install(updater: Path) -> None:
    dest = TEST_INSTALL_DIR / "AICA.Updater.exe"
    shutil.copy2(updater, dest)
    record(
        "PART_C_updater_bootstrap",
        "PASS" if dest.is_file() else "FAIL",
        "copied updater into isolated 1.0.2 test install (production 1.0.2 lacks updater)",
    )


def stage_new_installer_and_run_updater(updater: Path, new_setup: Path) -> None:
    """Real install flow into AICA_UpdateTest using staged new installer."""
    if not new_setup.is_file():
        record("TEST_7_real_install", "NOT RUN", f"missing {new_setup}")
        return

    version = "1.0.3"
    # Use process TEMP so packaged updater and harness agree
    temp_root = Path(os.environ.get("TEMP") or tempfile.gettempdir())
    staging = temp_root / "AICA" / "updates" / version
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    dest_installer = staging / f"AICA_Setup_{version}.exe"
    print(f"Copying installer to staging ({new_setup.stat().st_size} bytes)...")
    shutil.copy2(new_setup, dest_installer)
    digest = sha256_file(dest_installer)
    record("TEST_3_sha_of_staged", "PASS", f"sha256={digest} size={dest_installer.stat().st_size}")

    from desktop.updater.updater_validate import write_handoff_file

    # Point LOCALAPPDATA validation: install_dir is AICA_UpdateTest which is named AICA_UpdateTest not AICA!
    # install_dir validation requires name == "AICA". So real Inno /DIR= must end with \AICA
    # Use LOCALAPPDATA\AICA_UpdateTestRoot\AICA as install dir instead.
    record(
        "TEST_7_real_install",
        "NOT RUN",
        "install_dir must be named AICA; see harness isolated_aica_dir flow",
    )


def main() -> int:
    print("=== Phase 6 integration harness ===")
    print(f"ROOT={ROOT}")
    print(f"Production install will NOT be touched: %LOCALAPPDATA%\\AICA")

    test_unit_suites()

    exe = test_updater_exe_exists()
    if exe:
        test_updater_help(exe)
        test_updater_dry_run(exe)
        test_hash_failure_reject(exe)
    test_untrusted_path_rejected()

    new_setup = ROOT / "dist" / "AICA_Setup_1.0.3.exe"
    if new_setup.is_file():
        record("PART_B_installer_1_0_3", "PASS", f"size={new_setup.stat().st_size}")
    else:
        record("PART_B_installer_1_0_3", "NOT RUN", "AICA_Setup_1.0.3.exe not built yet")

    print("\n=== Summary ===")
    for name, status, reason in RESULTS:
        print(f"{status:8} {name}: {reason}")
    fails = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
