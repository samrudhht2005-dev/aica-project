"""Phase 5 — launch AICA.Updater.exe and gracefully shut down AICA."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from desktop.launcher.update_log import ulog

_apply_lock = threading.RLock()
_apply_state: dict[str, Any] = {
    "status": "idle",  # idle | applying | error
    "version": None,
    "error": None,
    "updater_started": False,
}
_shutdown_hook: Callable[[], None] | None = None
_engine_pid_provider: Callable[[], int | None] | None = None


def register_graceful_shutdown(hook: Callable[[], None]) -> None:
    """Registered by main.py to stop voice/engine and destroy WebView."""
    global _shutdown_hook
    _shutdown_hook = hook


def register_engine_pid_provider(provider: Callable[[], int | None]) -> None:
    global _engine_pid_provider
    _engine_pid_provider = provider


def get_apply_status_dict() -> dict[str, Any]:
    with _apply_lock:
        return {
            "status": _apply_state["status"],
            "version": _apply_state["version"],
            "error": _apply_state["error"],
            "updater_started": bool(_apply_state["updater_started"]),
            "active": _apply_state["status"] == "applying",
        }


def _set_apply_state(**kwargs: Any) -> None:
    with _apply_lock:
        _apply_state.update(kwargs)


def _install_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # Dev: pretend install dir is LocalAppData\AICA for path shape; updater dry-run tests override.
    local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(local) / "AICA"


def _restart_exe(install_dir: Path) -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return install_dir / "AICA.exe"


def resolve_updater_command() -> list[str] | None:
    """
    Prefer bundled AICA.Updater.exe beside the launcher.
    Dev fallback: python -m desktop.updater.main
    """
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).resolve().parent / "AICA.Updater.exe"
        if candidate.is_file():
            return [str(candidate)]
        return None

    # Development / tests
    root = Path(__file__).resolve().parents[2]
    return [sys.executable, "-m", "desktop.updater.main"]


def _ready_installer_info() -> tuple[dict[str, Any] | None, str | None]:
    from desktop.launcher.update_download import get_ready_installer_info

    return get_ready_installer_info()


def _schedule_shutdown() -> None:
    def _run() -> None:
        # Allow bridge response to reach the UI before tearing down WebView.
        time.sleep(0.6)
        hook = _shutdown_hook
        if hook:
            try:
                hook()
            except Exception as e:
                ulog("update_apply_shutdown_failed", error=str(e))
        else:
            ulog("update_apply_shutdown_missing_hook")
            try:
                os._exit(0)
            except Exception:
                pass

    threading.Thread(target=_run, name="aica-update-shutdown", daemon=True).start()


def apply_staged_update(*, dry_run: bool = False) -> dict[str, Any]:
    """
    Validate Phase-4 ready installer, write handoff, launch updater, then shut down.
    Returns immediately after successful updater launch (shutdown is async).
    Never accepts installer path/hash/URL from the frontend.
    """
    with _apply_lock:
        if _apply_state["status"] == "applying":
            payload = get_apply_status_dict()
            payload["ok"] = False
            payload["already_in_progress"] = True
            return payload

    info, err = _ready_installer_info()
    if info is None:
        msg = "Update is not ready for installation."
        _set_apply_state(status="error", error=msg, updater_started=False)
        ulog("update_apply_not_ready", error=err or "")
        return {
            "ok": False,
            "status": "error",
            "error": msg,
            "updater_started": False,
            "already_in_progress": False,
        }

    version = str(info["version"])
    sha256 = str(info["sha256"])
    staging = Path(info["staging_dir"])
    installer_path = Path(info["installer_path"])

    if not installer_path.is_file():
        msg = "Verified installer is no longer available."
        _set_apply_state(status="error", version=version, error=msg, updater_started=False)
        return {
            "ok": False,
            "status": "error",
            "error": msg,
            "updater_started": False,
            "already_in_progress": False,
        }

    updater_cmd = resolve_updater_command()
    if not updater_cmd:
        msg = "Updater is not available. Please reinstall AICA."
        _set_apply_state(status="error", version=version, error=msg, updater_started=False)
        ulog("update_apply_updater_missing")
        return {
            "ok": False,
            "status": "error",
            "error": msg,
            "updater_started": False,
            "already_in_progress": False,
        }

    install_dir = _install_dir()
    restart_exe = _restart_exe(install_dir)
    engine_pid = None
    if _engine_pid_provider:
        try:
            engine_pid = _engine_pid_provider()
        except Exception:
            engine_pid = None

    from desktop.updater.updater_validate import write_handoff_file

    try:
        handoff_path = write_handoff_file(
            staging_dir=staging,
            target_version=version,
            sha256=sha256,
            aica_pid=os.getpid(),
            engine_pid=engine_pid,
            install_dir=install_dir,
            restart_exe=restart_exe,
            dry_run=dry_run,
        )
    except Exception as e:
        msg = "Unable to prepare the update."
        _set_apply_state(status="error", version=version, error=msg, updater_started=False)
        ulog("update_apply_handoff_write_failed", error=str(e))
        return {
            "ok": False,
            "status": "error",
            "error": msg,
            "updater_started": False,
            "already_in_progress": False,
        }

    _set_apply_state(status="applying", version=version, error=None, updater_started=False)

    cmd = list(updater_cmd) + ["--handoff", str(handoff_path)]
    if dry_run:
        cmd.append("--dry-run")

    creationflags = 0
    if os.name == "nt":
        # Detach so updater survives AICA exit.
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path(updater_cmd[0]).resolve().parent) if Path(updater_cmd[0]).suffix.lower() == ".exe" else None,
            creationflags=creationflags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        msg = "Unable to start the updater. AICA will stay open."
        _set_apply_state(status="error", version=version, error=msg, updater_started=False)
        ulog("update_apply_launch_failed", error=str(e))
        return {
            "ok": False,
            "status": "error",
            "error": msg,
            "updater_started": False,
            "already_in_progress": False,
        }

    # Confirm process actually started.
    time.sleep(0.15)
    if proc.poll() is not None and proc.returncode not in (None,):
        # Immediate exit with failure — keep AICA alive.
        if proc.returncode != 0:
            msg = "Updater exited immediately. AICA will stay open."
            _set_apply_state(status="error", version=version, error=msg, updater_started=False)
            ulog("update_apply_launch_exited", code=proc.returncode)
            return {
                "ok": False,
                "status": "error",
                "error": msg,
                "updater_started": False,
                "already_in_progress": False,
            }

    _set_apply_state(status="applying", version=version, error=None, updater_started=True)
    ulog("update_apply_updater_launched", version=version, pid=proc.pid)

    if not dry_run:
        _schedule_shutdown()

    return {
        "ok": True,
        "status": "applying",
        "version": version,
        "updater_started": True,
        "already_in_progress": False,
        "error": None,
        # Intentionally no filesystem paths.
    }
