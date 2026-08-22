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
        msg = plain_text_for_speech(text)
        if not msg:
            return {"ok": True, "spoken": ""}
        with self._lock:
            try:
                synth = self._ensure()
                synth.Speak(msg)  # synchronous — blocks until done
                vlog("tts_spoke", chars=len(msg))
                return {"ok": True, "spoken": msg}
            except Exception as e:
                vlog("tts_failed", error=str(e))
                return {"ok": False, "error": str(e)}

    def dispose(self) -> None:
        with self._lock:
            if self._synth is not None:
                try:
                    self._synth.Dispose()
                except Exception:
                    pass
                self._synth = None
