"""Two-stage wake verification — embedding score + bounded Whisper on short clips."""
from __future__ import annotations

import os
from typing import Callable

from desktop.launcher.voice_intents import detect_wake
from desktop.launcher.voice_log import vlog

# Strong embedding hit — fire immediately without Whisper.
STRONG_MARGIN_DEFAULT = 0.02
# Below strong but above this on a short clip → Stage-2 Whisper verify once.
AMBIGUOUS_MARGIN_LOW = -0.06
# Only verify short utterances (standalone wake), not long nav commands.
MAX_WHISPER_VERIFY_DURATION_S = float(os.environ.get("AICA_WAKE_VERIFY_MAX_S", "2.8"))


def is_standalone_here_mishear(text: str) -> bool:
    """
    Whisper often transcribes standalone 'Hey Ira' as 'Here.' on live mic.
    Only valid for short wake clips verified by the caller (not general chat).
    """
    import re

    t = re.sub(r"[^\w\s]", " ", (text or "").lower()).strip()
    words = [w for w in t.split() if w]
    return len(words) == 1 and words[0] == "here"


def verify_wake_transcript(text: str, *, allow_here_mishear: bool = False) -> bool:
    if detect_wake(text):
        return True
    if allow_here_mishear and is_standalone_here_mishear(text):
        return True
    return False


def pcm_duration_s(pcm: bytes) -> float:
    # int16 mono 16 kHz
    return len(pcm) / (16000 * 2) if pcm else 0.0


def try_wake_on_pcm(
    pcm: bytes,
    score_fn: Callable[[bytes], tuple[float, float, float]],
    *,
    strong_margin: float = STRONG_MARGIN_DEFAULT,
) -> tuple[bool, float, str, str]:
    """
    Returns (fired, confidence_score, method, whisper_text).
    method: embedding | whisper_verify | none
    """
    if not pcm:
        return False, 0.0, "none", ""

    wake_sim, neg_sim, margin = score_fn(pcm)
    duration = pcm_duration_s(pcm)

    if margin >= strong_margin:
        vlog(
            "wake_strong_embed",
            margin=round(margin, 4),
            duration_s=round(duration, 2),
        )
        return True, max(0.0, min(1.0, 0.5 + margin * 5.0)), "embedding", ""

    if duration > MAX_WHISPER_VERIFY_DURATION_S:
        return False, margin, "none", ""

    if margin < AMBIGUOUS_MARGIN_LOW:
        return False, margin, "none", ""

    try:
        from desktop.launcher.voice_stt import WhisperSTT

        result = WhisperSTT.get().transcribe_pcm(pcm)
        text = (result.get("text") or "").strip()
        vlog(
            "wake_whisper_verify",
            margin=round(margin, 4),
            text=text,
            duration_s=round(duration, 2),
            ms=result.get("latency_ms"),
        )
        if verify_wake_transcript(text, allow_here_mishear=True):
            conf = result.get("confidence")
            score = float(conf) if conf is not None else 0.75
            return True, score, "whisper_verify", text
    except Exception as e:
        vlog("wake_whisper_verify_error", error=str(e))

    return False, margin, "none", ""


def evaluate_wake_pcm(pcm: bytes) -> dict:
    """Benchmark + diagnostics — uses production WakeDetector backend."""
    from desktop.launcher.voice_wake import WakeDetector
    from desktop.launcher.voice_wake_embed import EmbeddingWakeDetector

    det = WakeDetector()
    det.ensure_loaded()
    backend = det._mode

    if isinstance(det._backend, EmbeddingWakeDetector):
        fired, score, method, wx = try_wake_on_pcm(
            pcm,
            det._backend._score_pcm,
            strong_margin=float(det._backend._margin_threshold or STRONG_MARGIN_DEFAULT),
        )
        ws, ns, margin = det._backend._score_pcm(pcm)
        return {
            "wake_detected": fired,
            "wake_embedding_detected": method == "embedding",
            "wake_transcript_detected": method == "whisper_verify",
            "wake_embedding_margin": margin,
            "wake_verify_transcript": wx,
            "wake_backend": backend,
            "wake_method": method,
            "wake_score": score,
        }

    # vad_whisper / oww fallback — score via whisper on clip
    from desktop.launcher.voice_stt import WhisperSTT

    out = WhisperSTT.get().transcribe_pcm(pcm)
    text = (out.get("text") or "").strip()
    ok = verify_wake_transcript(text, allow_here_mishear=True)
    return {
        "wake_detected": ok,
        "wake_embedding_detected": False,
        "wake_transcript_detected": ok,
        "wake_embedding_margin": None,
        "wake_verify_transcript": text,
        "wake_backend": backend,
        "wake_method": "whisper_verify" if ok else "none",
        "wake_score": out.get("confidence"),
    }
