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
    Packaged desktop defaults to persistent SQLite under AppData (or AICA_APPDATA).
    Explicit postgresql:// DATABASE_URL is still honored for web/dev.
    """
    load_runtime_env()
    url = resolve_database_url()
    if url:
        os.environ["DATABASE_URL"] = url
        return True
    if is_frozen() or os.environ.get("AICA_DESKTOP") == "1":
        from backend.runtime_paths import desktop_sqlite_url
        os.environ["DATABASE_URL"] = desktop_sqlite_url()
        return True
    return False


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


def whisper_minimal_test() -> int:
    """Minimal packaged/dev Whisper test — load model, transcribe bundled PCM, exit."""
    import json
    from pathlib import Path

    from desktop.launcher.voice_diag import vdiag
    from desktop.launcher.voice_paths import faster_whisper_assets_dir, whisper_model_dir
    from desktop.launcher.voice_stt import WhisperSTT

    def _log(body: str) -> None:
        try:
            p = logs_dir() / "whisper_minimal_test.log"
            p.write_text(body, encoding="utf-8")
        except Exception:
            pass

    vdiag("MINIMAL_TEST_START", argv=list(sys.argv))
    model_path = whisper_model_dir()
    vad_path = faster_whisper_assets_dir() / "silero_vad_v6.onnx"
    vdiag(
        "MINIMAL_TEST_PATHS",
        model_path=str(model_path),
        model_bin=(model_path / "model.bin").is_file(),
        vad_path=str(vad_path),
        vad_exists=vad_path.is_file(),
        meipass=getattr(sys, "_MEIPASS", None),
    )

    pcm_dirs: list[Path] = [_REPO / "desktop" / "voice" / "assets"]
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            pcm_dirs.insert(0, Path(meipass) / "desktop" / "voice" / "assets")
        pcm_dirs.insert(0, Path(sys.executable).resolve().parent / "voice" / "assets")

    pcm: bytes | None = None
    for d in pcm_dirs:
        candidate = d / "selftest_open_expenses.pcm"
        if candidate.is_file():
            pcm = candidate.read_bytes()
            vdiag("MINIMAL_TEST_PCM", path=str(candidate), bytes=len(pcm))
            break
    if not pcm:
        vdiag("MINIMAL_TEST_FAIL", reason="missing_pcm")
        _log("FAIL missing selftest_open_expenses.pcm\n")
        return 1

    stt = WhisperSTT.get()
    stt.ensure_loaded()
    out = stt.transcribe_pcm(pcm)
    text = (out.get("text") or "").strip()
    ok = bool(text)
    result = "WHISPER_MINIMAL_OK" if ok else "WHISPER_MINIMAL_FAIL"
    vdiag("MINIMAL_TEST_DONE", text=text, ok=ok, latency_ms=out.get("latency_ms"))
    body = json.dumps(
        {
            "result": result,
            "text": text,
            "latency_ms": out.get("latency_ms"),
            "model_path": str(model_path),
            "segments": len(out.get("segments") or []),
        },
        indent=2,
    )
    _log(body + "\n")
    print(result, text)
    return 0 if ok else 1


def voice_selftest() -> int:
    """CLI self-test for packaged voice stack (no WebView)."""
    import audioop
    import json
    import tempfile
    import wave

    from desktop.launcher.voice_diag import vdiag
    from desktop.launcher.voice_engine import ModernVoiceEngine
    from desktop.launcher.voice_intents import match_intent
    from desktop.launcher.voice_stt import WhisperSTT
    from desktop.launcher.voice_tts import NativeTTS

    def _write_log(body: str) -> None:
        try:
            from backend.runtime_paths import logs_dir

            (logs_dir() / "voice_selftest.log").write_text(body, encoding="utf-8")
        except Exception:
            pass

    def wav_to_pcm16_16k(wav_path: Path) -> bytes:
        with wave.open(str(wav_path), "rb") as wf:
            rate = wf.getframerate()
            width = wf.getsampwidth()
            pcm = wf.readframes(wf.getnframes())
            if width != 2:
                pcm = audioop.lin2lin(pcm, width, 2)
            if wf.getnchannels() == 2:
                pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
            if rate != 16000:
                pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 16000, None)
            return pcm

    def synth_wav(text: str, path: Path) -> None:
        import clr  # type: ignore

        clr.AddReference(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\WPF\System.Speech.dll")
        clr.AddReference("System")
        from System.IO import FileStream, FileMode  # type: ignore
        from System.Speech.Synthesis import SpeechSynthesizer  # type: ignore

        synth = SpeechSynthesizer()
        stream = FileStream(str(path), FileMode.Create)
        try:
            synth.SelectVoice("Microsoft Zira Desktop")
            synth.SetOutputToWaveStream(stream)
            synth.Speak(text)
        finally:
            synth.SetOutputToNull()
            stream.Close()
            synth.Dispose()

    print("AICA voice self-test")
    vdiag("SELFTEST_START")
    _write_log("status=started\n")
    engine = ModernVoiceEngine()
    vdiag("SELFTEST_MIC_START")
    print("mic:", json.dumps(engine.mic_available()))
    vdiag("SELFTEST_WARMUP_START")
    warm = engine.warm_up()
    vdiag("SELFTEST_WARMUP_RETURN", ok=warm.get("ok"))
    print("warm_up:", json.dumps(warm))
    if not warm.get("ok"):
        _write_log(f"status=warm_up_failed\nwarm={json.dumps(warm)}\n")
        return 1

    def selftest_pcm() -> bytes:
        asset_dirs = []
        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                asset_dirs.append(Path(meipass) / "desktop" / "voice" / "assets")
            asset_dirs.append(Path(sys.executable).resolve().parent / "voice" / "assets")
        asset_dirs.append(_REPO / "desktop" / "voice" / "assets")
        for d in asset_dirs:
            pcm_path = d / "selftest_open_expenses.pcm"
            if pcm_path.is_file():
                return pcm_path.read_bytes()
        wav = Path(tempfile.mkdtemp()) / "stt.wav"
        synth_wav("open expenses", wav)
        return wav_to_pcm16_16k(wav)

    pcm = selftest_pcm()
    vdiag("SELFTEST_PCM_READY", bytes=len(pcm))
    vdiag("SELFTEST_TRANSCRIBE_START")
    out = WhisperSTT.get().transcribe_pcm(pcm)
    vdiag("SELFTEST_TRANSCRIBE_RETURN", text=out.get("text"))
    print("whisper:", json.dumps({k: out[k] for k in ("text", "confidence", "latency_ms")}))
    vdiag("SELFTEST_INTENT_START")
    m = match_intent(out.get("text") or "")
    vdiag("SELFTEST_INTENT_RETURN", intent=m.intent.name if m else None)
    print("intent:", m.intent.name if m else None)

    vdiag("SELFTEST_TTS_START")
    spoke = NativeTTS().speak("Voice self test OK.")
    vdiag("SELFTEST_TTS_RETURN", ok=spoke.get("ok"))
    print("tts:", json.dumps(spoke))
    ok = spoke.get("ok") and m is not None
    result = "VOICE_SELFTEST_OK" if ok else "VOICE_SELFTEST_FAIL"
    print(result)
    vdiag("SELFTEST_COMPLETE", result=result)
    _write_log(
        f"mic={json.dumps(engine.mic_available())}\n"
        f"warm={json.dumps(warm)}\n"
        f"whisper={json.dumps(out)}\n"
        f"intent={m.intent.name if m else None}\n"
        f"tts={json.dumps(spoke)}\n"
        f"result={result}\n"
    )
    return 0 if ok else 1


def main() -> int:
    if "--whisper-minimal-test" in sys.argv:
        return whisper_minimal_test()
    if "--voice-selftest" in sys.argv:
        return voice_selftest()
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
                "Desktop uses a local SQLite file by default. Optional DATABASE_URL overrides go in:\n"
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
