"""
AICA desktop entry: start local FastAPI (or attach to existing), open WebView2.

Dev:
  python -m desktop.launcher.main

Packaged:
  AICA.exe (this module frozen) starts AICA.Engine.exe then opens WebView2.
"""
from __future__ import annotations

import atexit
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# Allow running as `python -m desktop.launcher.main` from repo root
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from backend.runtime_paths import (  # noqa: E402
    APP_VERSION,
    APP_WINDOW_TITLE,
    appdata_dir,
    is_frozen,
    load_runtime_env,
    logs_dir,
    project_root,
    resolve_database_url,
    database_config_error_message,
    config_env_path,
)

ENGINE_HOST = "127.0.0.1"
READY_TIMEOUT_S = 180
POLL_S = 0.5

_engine_proc: subprocess.Popen | None = None


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((ENGINE_HOST, 0))
        return int(s.getsockname()[1])


def _health_url(port: int) -> str:
    return f"http://{ENGINE_HOST}:{port}/health"


def _wait_ready(port: int, timeout: float = READY_TIMEOUT_S) -> bool:
    deadline = time.time() + timeout
    url = _health_url(port)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(POLL_S)
    return False


def _engine_executable() -> Path | None:
    """Locate packaged FastAPI engine next to launcher."""
    if is_frozen():
        here = Path(sys.executable).resolve().parent
        candidates = [
            here / "engine" / "AICA.Engine.exe",
            here / "engine" / "AICA.Engine" / "AICA.Engine.exe",
            here / "AICA.Engine" / "AICA.Engine.exe",
            here / "AICA.Engine.exe",
            here.parent / "engine" / "AICA.Engine.exe",
        ]
        for c in candidates:
            if c.is_file():
                return c
        return None
    return None


def _ensure_database_url() -> bool:
    """
    Resolve DATABASE_URL for the engine child.
    Packaged local demo: if AppData is empty/placeholder, allow repo .env when
    AICA.exe lives under dist/ next to the project (never embeds secrets).
    """
    load_runtime_env()
    if resolve_database_url():
        return True
    if is_frozen():
        here = Path(sys.executable).resolve().parent
        for candidate in (
            here.parent / ".env",
            here.parent.parent / ".env",
            Path(os.environ.get("AICA_ENV_FILE") or ""),
        ):
            if candidate and candidate.is_file():
                os.environ["AICA_ENV_FILE"] = str(candidate)
                load_runtime_env()
                if resolve_database_url():
                    return True
    return bool(resolve_database_url())


def _start_engine(port: int) -> subprocess.Popen:
    global _engine_proc
    if not _ensure_database_url():
        raise RuntimeError(database_config_error_message())

    env = os.environ.copy()
    env["AICA_DESKTOP"] = "1"
    # Prefer packaged version.json so About/health match the release
    try:
        from backend.runtime_paths import app_release_info
        rel = app_release_info()
        env["AICA_VERSION"] = str(rel.get("version") or APP_VERSION)
        if rel.get("build"):
            env["AICA_BUILD"] = str(rel["build"])
    except Exception:
        env["AICA_VERSION"] = APP_VERSION
    env["AICA_HOST"] = ENGINE_HOST
    env["AICA_PORT"] = str(port)
    # Pass resolved URL explicitly so child never falls back to a template file
    db = resolve_database_url()
    if db:
        env["DATABASE_URL"] = db

    log_path = logs_dir() / "engine.log"
    log_f = open(log_path, "a", encoding="utf-8")

    exe = _engine_executable()
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    if exe:
        cmd = [str(exe)]
        cwd = str(exe.parent)
    else:
        py = sys.executable
        cmd = [
            py, "-m", "uvicorn",
            "backend.main:app",
            "--host", ENGINE_HOST,
            "--port", str(port),
            "--log-level", "info",
        ]
        cwd = str(project_root())
        env["PYTHONPATH"] = cwd + os.pathsep + env.get("PYTHONPATH", "")

    _engine_proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    return _engine_proc


def _stop_engine() -> None:
    global _engine_proc
    proc = _engine_proc
    _engine_proc = None
    if not proc:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass


def _show_error(message: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, "AICA", 0x10)
    except Exception:
        print(message, file=sys.stderr)


def main() -> int:
    t_launch = time.perf_counter()
    load_runtime_env()
    atexit.register(_stop_engine)

    if not _ensure_database_url():
        _show_error(database_config_error_message())
        return 1

    port = int(os.environ.get("AICA_PORT") or _find_free_port())
    os.environ["AICA_PORT"] = str(port)

    # If something already answers /health, reuse it (dev convenience)
    if not _wait_ready(port, timeout=0.8):
        try:
            t_engine = time.perf_counter()
            _start_engine(port)
        except Exception as e:
            _show_error(f"Could not start AICA engine.\n\n{e}\n\nSee logs in:\n{logs_dir()}")
            return 1
        if not _wait_ready(port):
            _show_error(
                "AICA engine started but did not become ready in time.\n\n"
                f"Check logs:\n{logs_dir() / 'engine.log'}\n\n"
                "Ensure a real DATABASE_URL (not USER/PASSWORD@HOST) is set in:\n"
                f"{config_env_path()}"
            )
            _stop_engine()
            return 1
        try:
            (logs_dir() / "startup_timing.log").write_text(
                f"engine_ready_s={time.perf_counter() - t_engine:.2f}\n"
                f"launcher_to_engine_ready_s={time.perf_counter() - t_launch:.2f}\n",
                encoding="utf-8",
            )
        except Exception:
            pass

    url = f"http://{ENGINE_HOST}:{port}/login"
    try:
        import webview
    except ImportError:
        _show_error(
            "pywebview is not installed.\n\n"
            "Development: pip install pywebview\n"
            "Packaged builds should include it."
        )
        _stop_engine()
        return 1

    from desktop.launcher.webview_desktop import (
        desktop_bootstrap_js,
        install_webview2_permission_hook,
        webview_user_data_dir,
    )
    from desktop.launcher.voice_bridge import get_voice_bridge

    # Remember Me / localStorage require a persistent WebView2 profile (not private_mode).
    install_webview2_permission_hook()
    storage = webview_user_data_dir()
    voice = get_voice_bridge()

    window = webview.create_window(
        APP_WINDOW_TITLE,
        url=url,
        width=1280,
        height=800,
        min_size=(960, 640),
        js_api=voice,
    )
    voice.attach_window(window)

    def _on_closed():
        try:
            voice.cancel_voice_listen()
        except Exception:
            pass
        _stop_engine()

    def _on_loaded():
        try:
            window.evaluate_js(desktop_bootstrap_js())
            # Surface mic backend status into the page for diagnostics
            try:
                info = voice.mic_available()
                window.evaluate_js(
                    "window.AICA_DESKTOP_MIC = "
                    + __import__("json").dumps(info)
                    + ";"
                )
            except Exception:
                pass
        except Exception as e:
            logging = __import__("logging")
            logging.warning("Desktop IRA bootstrap JS failed: %s", e)

    try:
        window.events.closed += _on_closed
    except Exception:
        pass
    try:
        window.events.loaded += _on_loaded
    except Exception:
        pass

    # private_mode=True (pywebview default) deletes cookies every launch — breaks Remember Me.
    webview.start(private_mode=False, storage_path=str(storage))
    _stop_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
