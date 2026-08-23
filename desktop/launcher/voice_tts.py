"""Windows native TTS via System.Speech.Synthesis."""
from __future__ import annotations

import re
import threading

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
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._synth = None
        self._speak_gen = 0

    def _ensure(self):
        if self._synth is not None:
            return self._synth
        import clr  # type: ignore

        clr.AddReference(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\WPF\System.Speech.dll")
        from System.Speech.Synthesis import SpeechSynthesizer  # type: ignore

        synth = SpeechSynthesizer()
        preferred = (
            "Microsoft Zira Desktop",
            "Microsoft David Desktop",
        )
        for name in preferred:
            try:
                synth.SelectVoice(name)
                break
            except Exception:
                continue
        synth.Rate = 0
        synth.Volume = 95
        self._synth = synth
        return synth

    def speak(self, text: str) -> dict:
        """Blocking speak (selftests / long replies). Lock is not held during Speak."""
        msg = plain_text_for_speech(text)
        if not msg:
            return {"ok": True, "spoken": ""}
        with self._lock:
            synth = self._ensure()
            gen = self._speak_gen
        try:
            synth.Speak(msg)
            vlog("tts_spoke", chars=len(msg), gen=gen)
            return {"ok": True, "spoken": msg}
        except Exception as e:
            vlog("tts_failed", error=str(e))
            return {"ok": False, "error": str(e)}

    def speak_async(self, text: str) -> dict:
        """
        Non-blocking short acknowledgement. Survives page navigation because it
        runs in the launcher process. Cancellable via cancel() / SpeakAsyncCancelAll.
        """
        msg = plain_text_for_speech(text)
        if not msg:
            return {"ok": True, "spoken": "", "async": True}
        with self._lock:
            try:
                synth = self._ensure()
                self._speak_gen += 1
                gen = self._speak_gen
                # Drop any prior async utterance, then queue this ack.
                try:
                    synth.SpeakAsyncCancelAll()
                except Exception:
                    pass
                synth.SpeakAsync(msg)
                vlog("tts_speak_async", chars=len(msg), gen=gen)
                return {"ok": True, "spoken": msg, "async": True, "gen": gen}
            except Exception as e:
                vlog("tts_speak_async_failed", error=str(e))
                return {"ok": False, "error": str(e), "async": True}

    def cancel(self) -> dict:
        """Stop any in-progress Speak/SpeakAsync immediately."""
        with self._lock:
            if self._synth is None:
                return {"ok": True, "cancelled": False}
            try:
                self._speak_gen += 1
                self._synth.SpeakAsyncCancelAll()
                vlog("tts_cancelled", gen=self._speak_gen)
                return {"ok": True, "cancelled": True}
            except Exception as e:
                vlog("tts_cancel_failed", error=str(e))
                return {"ok": False, "error": str(e)}

    def dispose(self) -> None:
        with self._lock:
            if self._synth is not None:
                try:
                    try:
                        self._synth.SpeakAsyncCancelAll()
                    except Exception:
                        pass
                    self._synth.Dispose()
                except Exception:
                    pass
                self._synth = None
