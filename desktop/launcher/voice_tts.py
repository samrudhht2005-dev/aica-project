"""Native IRA TTS — Piper neural voice (en_US-amy-medium) with SAPI fallback."""
from __future__ import annotations

import io
import re
import threading
import time
import wave
from typing import Any

from desktop.launcher.voice_log import vlog


def plain_text_for_speech(text: str) -> str:
    t = str(text or "")
    t = re.sub(r"```[\s\S]*?```", " ", t)
    t = re.sub(r"`[^`]*`", " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"[#>*_~]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


class NativeTTS:
    """
    Production TTS facade.

    Primary: Piper en_US-amy-medium (offline ONNX) + sounddevice playback.
    Fallback: Windows System.Speech (Zira/David) if Piper model/runtime unavailable.

    Cancellation uses a monotonic generation id so a superseded utterance cannot
    resume or overwrite a newer one. Audible stop is via sounddevice.stop()
    (Piper) or SpeakAsyncCancelAll (SAPI).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._speak_gen = 0
        self._engine: str | None = None  # "piper" | "sapi"
        self._piper = None
        self._piper_sample_rate = 22050
        self._sapi = None
        self._play_thread: threading.Thread | None = None
        self._busy = threading.Event()
        self._load_error: str | None = None

    # --- lifecycle ---------------------------------------------------------

    def ensure_loaded(self) -> dict[str, Any]:
        """Load Piper once (warm). Safe to call from warm_up; not on UI thread critical path."""
        with self._lock:
            if self._engine == "piper" and self._piper is not None:
                return {"ok": True, "engine": "piper", "already": True}
            if self._engine == "sapi" and self._sapi is not None:
                return {"ok": True, "engine": "sapi", "already": True, "fallback": True}
        return self._load_engine()

    def _load_engine(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            from desktop.launcher.voice_paths import (
                piper_espeak_data_dir,
                piper_voice_config_path,
                piper_voice_model_path,
            )

            model = piper_voice_model_path()
            config = piper_voice_config_path()
            if not model.is_file():
                raise FileNotFoundError(f"piper_model_missing:{model}")
            from piper import PiperVoice

            espeak = piper_espeak_data_dir()
            kwargs: dict[str, Any] = {"config_path": config if config.is_file() else None}
            if espeak.is_dir():
                kwargs["espeak_data_dir"] = espeak
            voice = PiperVoice.load(model, use_cuda=False, **kwargs)
            with self._lock:
                self._piper = voice
                self._piper_sample_rate = int(getattr(voice.config, "sample_rate", 22050) or 22050)
                self._engine = "piper"
                self._load_error = None
            elapsed = time.perf_counter() - t0
            vlog(
                "tts_piper_loaded",
                seconds=round(elapsed, 2),
                model=str(model),
                sample_rate=self._piper_sample_rate,
            )
            return {"ok": True, "engine": "piper", "warm_up_s": round(elapsed, 2)}
        except Exception as e:
            vlog("tts_piper_load_failed", error=str(e), fallback="sapi")
            self._load_error = str(e)
            try:
                self._ensure_sapi()
                with self._lock:
                    self._engine = "sapi"
                return {
                    "ok": True,
                    "engine": "sapi",
                    "fallback": True,
                    "error": str(e),
                }
            except Exception as e2:
                vlog("tts_sapi_fallback_failed", error=str(e2))
                return {"ok": False, "error": str(e2), "piper_error": str(e)}

    def _ensure_sapi(self):
        with self._lock:
            if self._sapi is not None:
                return self._sapi
        import clr  # type: ignore

        clr.AddReference(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\WPF\System.Speech.dll")
        from System.Speech.Synthesis import SpeechSynthesizer  # type: ignore

        synth = SpeechSynthesizer()
        for name in ("Microsoft Zira Desktop", "Microsoft David Desktop"):
            try:
                synth.SelectVoice(name)
                break
            except Exception:
                continue
        synth.Rate = 0
        synth.Volume = 95
        with self._lock:
            self._sapi = synth
            if self._engine is None:
                self._engine = "sapi"
            return self._sapi

    # --- synthesis / playback ----------------------------------------------

    def _synthesize_piper_float32(self, text: str):
        import numpy as np

        assert self._piper is not None
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            self._piper.synthesize_wav(text, wf)
        buf.seek(0)
        with wave.open(buf, "rb") as wf:
            rate = wf.getframerate()
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
        if sampwidth != 2:
            raise RuntimeError(f"unexpected_piper_sampwidth:{sampwidth}")
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            audio = audio.reshape(-1, channels)
        return audio, int(rate)

    def _stop_playback_unlocked(self) -> None:
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:
            pass
        self._busy.clear()

    def _play_float32(self, audio, sample_rate: int, gen: int, *, blocking: bool) -> None:
        import sounddevice as sd

        def _run() -> None:
            self._busy.set()
            try:
                with self._lock:
                    if gen != self._speak_gen:
                        return
                sd.play(audio, samplerate=sample_rate, blocking=False)
                # Poll until finished or superseded.
                while True:
                    with self._lock:
                        if gen != self._speak_gen:
                            try:
                                sd.stop()
                            except Exception:
                                pass
                            return
                    stream = None
                    try:
                        stream = sd.get_stream()
                    except Exception:
                        stream = None
                    if stream is None or not getattr(stream, "active", False):
                        break
                    time.sleep(0.02)
                try:
                    sd.wait()
                except Exception:
                    pass
            finally:
                with self._lock:
                    if gen == self._speak_gen:
                        self._busy.clear()

        if blocking:
            _run()
            return
        thr = threading.Thread(target=_run, name="aica-tts-play", daemon=True)
        with self._lock:
            self._play_thread = thr
        thr.start()

    def is_busy(self) -> bool:
        return self._busy.is_set()

    def wait_idle(self, timeout: float = 2.5) -> bool:
        """Wait until playback finishes (short). Used before ambient wake restart."""
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            if not self._busy.is_set():
                return True
            time.sleep(0.05)
        return not self._busy.is_set()

    # --- public API (stable) -----------------------------------------------

    def speak(self, text: str) -> dict:
        """Blocking speak (chat replies / selftests)."""
        msg = plain_text_for_speech(text)
        if not msg:
            return {"ok": True, "spoken": ""}
        loaded = self.ensure_loaded()
        if not loaded.get("ok"):
            return {"ok": False, "error": loaded.get("error") or "tts_unavailable"}

        with self._lock:
            self._speak_gen += 1
            gen = self._speak_gen
            self._stop_playback_unlocked()
            engine = self._engine

        try:
            if engine == "piper" and self._piper is not None:
                audio, rate = self._synthesize_piper_float32(msg)
                with self._lock:
                    if gen != self._speak_gen:
                        return {"ok": True, "spoken": msg, "cancelled": True, "engine": "piper"}
                self._play_float32(audio, rate, gen, blocking=True)
                vlog("tts_spoke", chars=len(msg), gen=gen, engine="piper")
                return {"ok": True, "spoken": msg, "engine": "piper", "gen": gen}

            synth = self._ensure_sapi()
            synth.Speak(msg)
            vlog("tts_spoke", chars=len(msg), gen=gen, engine="sapi")
            return {"ok": True, "spoken": msg, "engine": "sapi", "gen": gen}
        except Exception as e:
            vlog("tts_failed", error=str(e))
            return {"ok": False, "error": str(e)}

    def speak_async(self, text: str) -> dict:
        """
        Non-blocking short acknowledgement. Survives page navigation because it
        runs in the launcher process. Cancellable via cancel().
        """
        msg = plain_text_for_speech(text)
        if not msg:
            return {"ok": True, "spoken": "", "async": True}
        loaded = self.ensure_loaded()
        if not loaded.get("ok"):
            return {"ok": False, "error": loaded.get("error") or "tts_unavailable", "async": True}

        with self._lock:
            self._speak_gen += 1
            gen = self._speak_gen
            self._stop_playback_unlocked()
            engine = self._engine

        try:
            if engine == "piper" and self._piper is not None:
                audio, rate = self._synthesize_piper_float32(msg)
                with self._lock:
                    if gen != self._speak_gen:
                        return {
                            "ok": True,
                            "spoken": msg,
                            "async": True,
                            "cancelled": True,
                            "engine": "piper",
                            "gen": gen,
                        }
                self._play_float32(audio, rate, gen, blocking=False)
                vlog("tts_speak_async", chars=len(msg), gen=gen, engine="piper")
                return {"ok": True, "spoken": msg, "async": True, "engine": "piper", "gen": gen}

            synth = self._ensure_sapi()
            with self._lock:
                try:
                    synth.SpeakAsyncCancelAll()
                except Exception:
                    pass
                synth.SpeakAsync(msg)
            vlog("tts_speak_async", chars=len(msg), gen=gen, engine="sapi")
            return {"ok": True, "spoken": msg, "async": True, "engine": "sapi", "gen": gen}
        except Exception as e:
            vlog("tts_speak_async_failed", error=str(e))
            return {"ok": False, "error": str(e), "async": True}

    def cancel(self) -> dict:
        """Stop any in-progress speech immediately and invalidate in-flight gens."""
        with self._lock:
            self._speak_gen += 1
            gen = self._speak_gen
            self._stop_playback_unlocked()
            sapi = self._sapi
        if sapi is not None:
            try:
                sapi.SpeakAsyncCancelAll()
            except Exception:
                pass
        vlog("tts_cancelled", gen=gen, engine=self._engine or "none")
        return {"ok": True, "cancelled": True, "gen": gen}

    def dispose(self) -> None:
        with self._lock:
            self._speak_gen += 1
            self._stop_playback_unlocked()
            self._piper = None
            if self._sapi is not None:
                try:
                    try:
                        self._sapi.SpeakAsyncCancelAll()
                    except Exception:
                        pass
                    self._sapi.Dispose()
                except Exception:
                    pass
                self._sapi = None
            self._engine = None
