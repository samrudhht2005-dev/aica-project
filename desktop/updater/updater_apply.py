"""Apply verified staged installer: wait → install → validate → restart."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from desktop.updater.updater_log import ulog
from desktop.updater.updater_validate import (
    Handoff,
    build_installer_command,
    load_and_validate_handoff,
    read_installed_version,
    reverify_installer,
)

# Prefer graceful wait; only terminate the exact engine PID from handoff after timeout.
DEFAULT_WAIT_TIMEOUT_S = 120.0
INSTALLER_TIMEOUT_S = 1800.0  # large installers (600+ MB) need headroom
POLL_INTERVAL_S = 0.5


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_for_pid(pid: int, *, timeout_s: float, label: str) -> bool:
    """Return True if process exited (or was already gone)."""
    if not _pid_exists(pid):
        ulog(f"{label}_already_exited", pid=pid)
        return True
    ulog(f"waiting_for_{label}", pid=pid, timeout_s=timeout_s)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            ulog(f"{label}_exit_detected", pid=pid)
            return True
        time.sleep(POLL_INTERVAL_S)
    ulog("process_wait_timeout", label=label, pid=pid)
    return False


def _terminate_pid(pid: int, *, label: str) -> None:
    """Last-resort terminate of a specific known PID only (never by image name)."""
    if pid <= 0 or not _pid_exists(pid):
        return
    ulog("process_force_terminate", label=label, pid=pid, reason="wait_timeout")
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except Exception as e:
            ulog("process_force_terminate_failed", label=label, pid=pid, error=str(e))
        return
    try:
        os.kill(pid, 15)
        time.sleep(1)
        if _pid_exists(pid):
            os.kill(pid, 9)
    except OSError as e:
        ulog("process_force_terminate_failed", label=label, pid=pid, error=str(e))


def wait_for_aica_processes(handoff: Handoff, *, timeout_s: float = DEFAULT_WAIT_TIMEOUT_S) -> None:
    ulog("waiting_for_aica_exit", pid=handoff.aica_pid)
    if not _wait_for_pid(handoff.aica_pid, timeout_s=timeout_s, label="aica"):
        # Do not kill the main AICA process by force — installer CloseApplications may help.
        ulog("aica_still_running_after_timeout", pid=handoff.aica_pid)

    if handoff.engine_pid:
        if not _wait_for_pid(handoff.engine_pid, timeout_s=min(30.0, timeout_s), label="engine"):
            # Documented force path: only the exact engine PID from handoff.
            _terminate_pid(handoff.engine_pid, label="engine")
            _wait_for_pid(handoff.engine_pid, timeout_s=10.0, label="engine")


def run_installer(handoff: Handoff, *, timeout_s: float = INSTALLER_TIMEOUT_S) -> int:
    cmd = build_installer_command(handoff.installer_path, handoff.install_dir)
    # Optional Inno log beside the staged installer (helps diagnose silent failures).
    log_path = handoff.installer_path.with_suffix(handoff.installer_path.suffix + ".inno.log")
    cmd = list(cmd) + [f"/LOG={log_path}"]
    ulog(
        "installer_started",
        version=handoff.target_version,
        args=["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/FORCECLOSEAPPLICATIONS", "/DIR=...", "/LOG=..."],
        dry_run=handoff.dry_run,
    )
    if handoff.dry_run:
        ulog("installer_dry_run_skip", command_preview=cmd[1:])
        return 0

    # Do NOT use CREATE_NO_WINDOW for Inno Setup — silent installs can abort
    # immediately (exit code 5) when launched from a windowed parent with that flag.
    try:
        completed = subprocess.run(
            cmd,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        ulog("installer_timeout", version=handoff.target_version)
        return 124
    except OSError as e:
        ulog("installer_start_failed", error=str(e))
        return 125

    code = int(completed.returncode if completed.returncode is not None else 1)
    ulog("installer_exit", code=code, version=handoff.target_version)
    if code != 0:
        ulog("installer_log_hint", log_name=log_path.name)
    return code


def post_install_validate(handoff: Handoff) -> str | None:
    ulog("post_install_validation_started", version=handoff.target_version)
    if handoff.dry_run:
        ulog("post_install_validation_dry_run_ok", version=handoff.target_version)
        return None

    if not handoff.restart_exe.is_file():
        ulog("post_install_validation_failed", reason="missing_aica_exe")
        return "missing_aica_exe"

    installed = read_installed_version(handoff.install_dir)
    if installed is None:
        ulog("post_install_validation_failed", reason="version_unreadable")
        return "version_unreadable"
    if installed != handoff.target_version:
        ulog(
            "post_install_validation_failed",
            reason="version_mismatch",
            expected=handoff.target_version,
            actual=installed,
        )
        return "version_mismatch"

    ulog("post_install_validation_ok", version=installed)
    return None


def restart_aica(handoff: Handoff) -> str | None:
    if handoff.dry_run:
        ulog("restart_dry_run_skip", exe=str(handoff.restart_exe.name))
        return None
    if not handoff.restart_exe.is_file():
        return "restart_exe_missing"
    ulog("restart_started", version=handoff.target_version)
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    try:
        subprocess.Popen(
            [str(handoff.restart_exe)],
            cwd=str(handoff.install_dir),
            creationflags=creationflags,
            close_fds=True,
        )
    except OSError as e:
        ulog("restart_failed", error=str(e))
        return "restart_failed"
    return None


def apply_from_handoff(
    handoff_path: Path,
    *,
    wait_timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
    installer_timeout_s: float = INSTALLER_TIMEOUT_S,
) -> int:
    """
    Full updater lifecycle. Returns process exit code.
    0 = success (or dry-run success), non-zero = failure.
    """
    ulog("updater_started", handoff=str(handoff_path.name))
    handoff, err = load_and_validate_handoff(handoff_path)
    if handoff is None:
        ulog("handoff_validated_failed", error=err)
        return 2
    ulog(
        "handoff_validated",
        version=handoff.target_version,
        aica_pid=handoff.aica_pid,
        dry_run=handoff.dry_run,
    )

    ulog("installer_rehash_started", version=handoff.target_version)
    rehash_err = reverify_installer(handoff)
    if rehash_err:
        ulog("installer_rehash_failed", error=rehash_err)
        return 3
    ulog("installer_rehash_ok", version=handoff.target_version)

    wait_for_aica_processes(handoff, timeout_s=wait_timeout_s)

    code = run_installer(handoff, timeout_s=installer_timeout_s)
    if code != 0:
        ulog("installer_failed", code=code)
        return 4 if code != 124 else 5

    val_err = post_install_validate(handoff)
    if val_err:
        return 6

    restart_err = restart_aica(handoff)
    if restart_err:
        return 7

    ulog("updater_completed", version=handoff.target_version)
    return 0
