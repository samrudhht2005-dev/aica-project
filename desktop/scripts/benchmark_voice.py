"""
Benchmark the CURRENT AICA desktop voice stack (System.Speech + assistant.js rules).

Does NOT modify production voice_bridge.py or assistant.js.
Results are written outside the application database:
  %AppData%\\AICA\\logs\\voice_benchmark_baseline.json

Modes:
  tts       — synthesize each phrase to WAV (en-US SAPI), feed DictationGrammar (automated)
  live      — microphone capture per phrase (interactive; speak when prompted)
  log       — score historical entries from voice.log
  all       — tts + log (default)
  whisper   — TTS -> faster-whisper (modern synthetic)

Examples:
  python desktop/scripts/benchmark_voice.py
  python desktop/scripts/benchmark_voice.py --mode live --backend legacy --trials 3
  python desktop/scripts/benchmark_voice.py --mode live --backend modern --trials 1
  python desktop/scripts/benchmark_voice.py --backend modern --mode whisper --trials 5
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import wave
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Mirror assistant.js (org UI mode)
WAKE_RE = re.compile(
    r"\b(hey|hay|hi|he)\s*[,.\-]?\s*(ira|aira|era|ara)\b|\bheira\b|\bhaira\b",
    re.I,
)

NAV_ROUTES: list[tuple[re.Pattern[str], str, str, str]] = [
    (re.compile(r"\b(open|go to|show|take me to)\s+(the\s+)?dashboard\b", re.I), "/", "Dashboard", "OPEN_DASHBOARD"),
    (re.compile(r"\b(open|go to|show)\s+(the\s+)?(expenses?|expense ledger)\b", re.I), "/expenses", "Expenses", "OPEN_EXPENSES"),
    (re.compile(r"\b(open|go to|show)\s+(the\s+)?(inventory|warehouse|stock)\b", re.I), "/warehouse", "Warehouse", "OPEN_INVENTORY"),
    (re.compile(r"\b(open|go to|show)\s+(the\s+)?(pos|point of sale|checkout|scanner)\b", re.I), "/pos", "POS", "OPEN_POS"),
    (re.compile(r"\b(open|go to|show|show me)\s+(the\s+)?(sales|sales analytics|analytics)\b", re.I), "/sales", "Sales", "OPEN_SALES"),
    (re.compile(r"\b(open|go to|show)\s+(the\s+)?(reports?)\b", re.I), "/reports", "Reports", "OPEN_REPORTS"),
    (re.compile(r"\b(open|go to|show)\s+(the\s+)?(organization|organisation|company|profile settings)\b", re.I), "/organization", "Organization", "OPEN_ORGANIZATION"),
    (re.compile(r"\b(switch to|open)\s+(organization|organisation)\s*(interface|mode)?\b", re.I), "/select-interface", "Interface", "OPEN_INTERFACE"),
    # User expects "switch to POS" / "open billing" — current app regex may not cover these:
    (re.compile(r"\b(switch to|go to)\s+(the\s+)?(pos|point of sale|billing|checkout)\b", re.I), "/pos", "POS", "OPEN_POS"),
    (re.compile(r"\b(open|go to|show)\s+(the\s+)?billing\b", re.I), "/pos", "POS", "OPEN_POS"),
]

CORE_PHRASES: list[tuple[str, str, str | None]] = [
    ("Hey Ira", "WAKE", None),
    ("Open expenses", "OPEN_EXPENSES", "/expenses"),
    ("Take me to expenses", "OPEN_EXPENSES", "/expenses"),
    ("Open sales", "OPEN_SALES", "/sales"),
    ("Take me to sales", "OPEN_SALES", "/sales"),
    ("Switch to POS", "OPEN_POS", "/pos"),
    ("Switch to organization", "OPEN_ORGANIZATION", "/organization"),
    ("Open dashboard", "OPEN_DASHBOARD", "/"),
    ("Open inventory", "OPEN_INVENTORY", "/warehouse"),
    ("Open billing", "OPEN_BILLING", "/pos"),
]

# Live modern mic benchmark — full utterances (wake + command where applicable)
MODERN_LIVE_PHRASES: list[tuple[str, str, str | None]] = [
    ("Hey Ira", "WAKE", None),
    ("Hey Ira, open expenses", "OPEN_EXPENSES", "/expenses"),
    ("Hey Ira, take me to sales", "OPEN_SALES", "/sales"),
    ("Hey Ira, switch to POS", "OPEN_POS", "/pos"),
    ("Hey Ira, switch to organization", "OPEN_ORGANIZATION", "/organization"),
    ("Hey Ira, open dashboard", "OPEN_DASHBOARD", "/"),
    ("Hey Ira, open inventory", "OPEN_INVENTORY", "/warehouse"),
    ("Hey Ira, open billing", "OPEN_BILLING", "/pos"),
    ("Hey Ira, open reports", "OPEN_REPORTS", "/reports"),
    ("Hey Ira, open analytics", "OPEN_ANALYTICS", "/sales"),
]


@dataclass
class TrialResult:
    spoken_phrase: str
    expected_intent: str
    expected_path: str | None
    mode: str
    trial_index: int
    voice_variant: str
    recognized_transcript: str
    confidence: float | None
    wake_detected: bool
    detected_intent: str | None
    detected_path: str | None
    intent_correct: bool
    stt_exact_match: bool
    latency_ms: float
    failure_reason: str | None = None
    notes: str | None = None


@dataclass
class BenchmarkReport:
    generated_at: str
    engine: str
    recognizer: str
    culture: str
    modes_run: list[str]
    trials: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    production_log_summary: dict[str, Any] = field(default_factory=dict)


def logs_dir() -> Path:
    try:
        from backend.runtime_paths import logs_dir as _logs

        return _logs()
    except Exception:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        d = base / "AICA" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d


def output_path() -> Path:
    return logs_dir() / "voice_benchmark_baseline.json"


def strip_wake(text: str) -> str:
    return WAKE_RE.sub(" ", text or "").strip()


def match_nav(text: str) -> tuple[str, str, str] | None:
    q = strip_wake(text)
    if not q:
        return None
    for pattern, path, label, intent in NAV_ROUTES:
        if pattern.search(q):
            return intent, path, label
    return None


def evaluate_transcript(spoken: str, expected_intent: str, expected_path: str | None, transcript: str) -> TrialResult:
    transcript = (transcript or "").strip()
    wake = bool(WAKE_RE.search(transcript))
    nav = match_nav(transcript)
    detected_intent = "WAKE" if wake and expected_intent == "WAKE" else (nav[0] if nav else ("WAKE" if wake else None))
    detected_path = nav[1] if nav else None

    if expected_intent == "WAKE":
        intent_ok = wake
        failure = None if intent_ok else "wake_regex_no_match"
    else:
        intent_ok = detected_intent == expected_intent
        if not transcript:
            failure = "empty_transcript"
        elif not nav:
            failure = "no_nav_regex_match"
        elif not intent_ok:
            failure = f"wrong_intent:{detected_intent}"
        else:
            failure = None

    norm_spoken = re.sub(r"\s+", " ", spoken.lower().strip())
    norm_transcript = re.sub(r"\s+", " ", strip_wake(transcript).lower().strip())
    exact = norm_transcript == norm_spoken or norm_transcript == norm_spoken.replace("hey ira", "").strip()

    return TrialResult(
        spoken_phrase=spoken,
        expected_intent=expected_intent,
        expected_path=expected_path,
        mode="eval",
        trial_index=0,
        voice_variant="",
        recognized_transcript=transcript,
        confidence=None,
        wake_detected=wake,
        detected_intent=detected_intent,
        detected_path=detected_path,
        intent_correct=intent_ok,
        stt_exact_match=exact,
        latency_ms=0.0,
        failure_reason=failure,
    )


def load_system_speech():
    from desktop.launcher.voice_legacy import _load_system_speech

    return _load_system_speech()


def try_get_recognizer_info() -> tuple[dict[str, str] | None, str | None]:
    """Return (info, skip_reason). skip_reason set when System.Speech/pythonnet unavailable."""
    try:
        return get_recognizer_info(), None
    except Exception:
        return None, "legacy dependency unavailable"


def get_modern_engine_info() -> dict[str, str]:
    from desktop.launcher.voice_paths import whisper_model_dir

    wake_mode = "unknown"
    try:
        from desktop.launcher.voice_wake import WakeDetector

        wd = WakeDetector()
        wd.ensure_loaded()
        wake_mode = wd._mode
    except Exception as e:
        wake_mode = f"error:{e}"
    return {
        "engine": "aica-voice-v2",
        "recognizer": "faster-whisper-small.en-cpu-int8",
        "culture": "en",
        "wake_backend": wake_mode,
        "whisper_model": str(whisper_model_dir()),
    }


def _score_wake_on_pcm(pcm: bytes) -> dict:
    """Production two-stage wake path on captured PCM."""
    from desktop.launcher.voice_wake_verify import evaluate_wake_pcm

    return evaluate_wake_pcm(pcm)


def _stt_phrase_match(spoken: str, transcript: str) -> bool:
    from desktop.launcher.voice_intents import normalize_transcript

    norm_spoken = normalize_transcript(spoken)
    norm_tx = normalize_transcript(transcript)
    if not norm_tx:
        return False
    if norm_tx == norm_spoken:
        return True
    # Allow minor punctuation / filler differences for live speech
    if norm_spoken in norm_tx or norm_tx in norm_spoken:
        return True
    spoken_tail = norm_spoken.replace("hey ira", "").strip()
    if spoken_tail and spoken_tail in norm_tx:
        return True
    return False


def summarize_modern_live(trials: list[dict[str, Any]]) -> dict[str, Any]:
    if not trials:
        return {}

    def _rate(key: str) -> float | None:
        if key == "wake":
            subset = [t for t in trials if t.get("expected_intent") == "WAKE"]
            if not subset:
                return None
            return round(sum(1 for t in subset if t.get("intent_correct")) / len(subset), 3)
        if key == "intent":
            subset = [t for t in trials if t.get("expected_intent") != "WAKE"]
            if not subset:
                return None
            return round(sum(1 for t in subset if t.get("intent_correct")) / len(subset), 3)
        if key == "stt":
            return round(sum(1 for t in trials if t.get("stt_exact_match")) / len(trials), 3)
        return None

    latencies = [float(t["latency_ms"]) for t in trials if t.get("latency_ms")]
    transcribe = [float(t["transcribe_ms"]) for t in trials if t.get("transcribe_ms")]
    first_tx = next((float(t["transcribe_ms"]) for t in trials if t.get("transcribe_ms")), None)
    rest_tx = [float(t["transcribe_ms"]) for t in trials[1:] if t.get("transcribe_ms")]

    by_phrase: dict[str, Any] = {}
    for spoken, _, _ in MODERN_LIVE_PHRASES:
        subset = [t for t in trials if t.get("spoken_phrase") == spoken]
        if not subset:
            continue
        by_phrase[spoken] = {
            "trials": len(subset),
            "intent_accuracy": round(sum(1 for t in subset if t.get("intent_correct")) / len(subset), 3),
            "wake_detected_rate": round(sum(1 for t in subset if t.get("wake_detected")) / len(subset), 3),
            "stt_exact_match_rate": round(sum(1 for t in subset if t.get("stt_exact_match")) / len(subset), 3),
            "transcripts": [t.get("recognized_transcript") for t in subset],
        }

    return {
        "trial_count": len(trials),
        "wake_accuracy": _rate("wake"),
        "intent_accuracy": _rate("intent"),
        "stt_exact_match_rate": _rate("stt"),
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "avg_transcribe_ms": round(sum(transcribe) / len(transcribe), 1) if transcribe else None,
        "first_transcribe_ms": round(first_tx, 1) if first_tx is not None else None,
        "subsequent_transcribe_ms": round(sum(rest_tx) / len(rest_tx), 1) if rest_tx else None,
        "model_load_ms": trials[0].get("model_load_ms") if trials else None,
        "by_phrase": by_phrase,
        "transcripts": [
            {
                "spoken": t.get("spoken_phrase"),
                "heard": t.get("recognized_transcript"),
                "normalized": t.get("normalized_transcript"),
                "expected": t.get("expected_intent"),
                "detected": t.get("detected_intent"),
                "wake": t.get("wake_detected"),
                "ok": t.get("intent_correct"),
            }
            for t in trials
        ],
    }


def run_modern_live_benchmark(trials_per_phrase: int) -> list[dict[str, Any]]:
    """
    Live mic benchmark using production modern stack only:
      sounddevice -> VAD -> wake embedding -> faster-whisper -> voice_intents
    Does NOT use System.Speech or recognize_live_mic().
    """
    from desktop.launcher.voice_audio import MicCapture, pcm_duration_ms
    from desktop.launcher.voice_intents import detect_wake, match_intent, normalize_transcript
    from desktop.launcher.voice_stt import WhisperSTT
    from desktop.launcher.voice_wake import WakeDetector

    mic = MicCapture()
    mic_info = mic.mic_available()
    if not mic_info.get("ok"):
        print("FAIL: microphone not available:", mic_info)
        return []

    print("\n=== MODERN LIVE MICROPHONE BENCHMARK ===")
    print("Stack: sounddevice -> VAD -> openWakeWord embeddings -> faster-whisper -> voice_intents")
    print(f"Mic: {mic_info.get('device')}")
    print("Speak naturally when prompted. Ctrl+C to stop.\n")

    t_load0 = time.perf_counter()
    stt = WhisperSTT.get()
    stt.ensure_loaded()
    stt.warm_transcribe()
    wake = WakeDetector()
    wake.ensure_loaded()
    model_load_ms = (time.perf_counter() - t_load0) * 1000.0
    modern_info = get_modern_engine_info()
    print(f"Models loaded in {model_load_ms:.0f} ms (wake={modern_info.get('wake_backend')})")

    results: list[dict[str, Any]] = []

    for spoken, expected_intent, expected_path in MODERN_LIVE_PHRASES:
        for trial in range(trials_per_phrase):
            print(f"[trial {trial + 1}/{trials_per_phrase}] Say naturally: \"{spoken}\"")
            try:
                t_total0 = time.perf_counter()
                t_cap0 = time.perf_counter()
                pcm = mic.record_until_silence(max_seconds=10.0, silence_ms=1400, min_utterance_ms=2800)
                capture_ms = (time.perf_counter() - t_cap0) * 1000.0
                audio_duration_ms = pcm_duration_ms(pcm)

                if not pcm:
                    results.append(
                        {
                            "spoken_phrase": spoken,
                            "expected_intent": expected_intent,
                            "expected_path": expected_path,
                            "mode": "modern_live",
                            "trial_index": trial + 1,
                            "voice_variant": "live_mic_modern",
                            "recognized_transcript": "",
                            "normalized_transcript": "",
                            "confidence": None,
                            "wake_detected": False,
                            "wake_embedding_detected": False,
                            "wake_transcript_detected": False,
                            "wake_embedding_margin": None,
                            "wake_backend": modern_info.get("wake_backend"),
                            "detected_intent": None,
                            "detected_path": None,
                            "intent_correct": False,
                            "stt_exact_match": False,
                            "latency_ms": (time.perf_counter() - t_total0) * 1000.0,
                            "capture_ms": capture_ms,
                            "transcribe_ms": 0.0,
                            "pcm_bytes": 0,
                            "model_load_ms": model_load_ms if not results else None,
                            "failure_reason": "empty_capture",
                        }
                    )
                    print("  (no speech captured)")
                    continue

                wake_eval = _score_wake_on_pcm(pcm)
                wake_embed_ok = bool(wake_eval.get("wake_detected"))
                wake_margin = wake_eval.get("wake_embedding_margin")
                wake_backend = wake_eval.get("wake_backend")
                wake_method = wake_eval.get("wake_method")
                wake_verify_tx = wake_eval.get("wake_verify_transcript") or ""

                t_tx0 = time.perf_counter()
                out = stt.transcribe_pcm(pcm)
                transcribe_ms = (time.perf_counter() - t_tx0) * 1000.0
                transcript = (out.get("text") or "").strip()
                if not transcript and wake_verify_tx:
                    transcript = wake_verify_tx.strip()
                conf = out.get("confidence")
                wake_tx_ok = detect_wake(transcript)
                detected_wake = wake_embed_ok or wake_tx_ok
                norm = normalize_transcript(transcript)

                if expected_intent == "WAKE":
                    intent_ok = detected_wake
                    detected_intent = "WAKE" if detected_wake else None
                    detected_path = None
                    failure = None if intent_ok else "wake_not_detected"
                else:
                    m = match_intent(transcript)
                    intent_ok = m is not None and m.intent.name == expected_intent
                    detected_intent = m.intent.name if m else None
                    detected_path = m.intent.path if m else None
                    if intent_ok:
                        failure = None
                    elif not transcript:
                        failure = "empty_transcript"
                    elif m is None:
                        failure = "no_intent_match"
                    else:
                        failure = f"wrong_intent:{detected_intent}"

                exact = _stt_phrase_match(spoken, transcript)
                latency_ms = (time.perf_counter() - t_total0) * 1000.0

                row = {
                    "spoken_phrase": spoken,
                    "expected_intent": expected_intent,
                    "expected_path": expected_path,
                    "mode": "modern_live",
                    "trial_index": trial + 1,
                    "voice_variant": "live_mic_modern",
                    "recognized_transcript": transcript,
                    "normalized_transcript": norm,
                    "confidence": conf,
                    "wake_detected": detected_wake,
                    "wake_embedding_detected": bool(wake_eval.get("wake_embedding_detected")),
                    "wake_transcript_detected": bool(wake_eval.get("wake_transcript_detected")) or wake_tx_ok,
                    "wake_method": wake_method,
                    "wake_verify_transcript": wake_verify_tx,
                    "wake_embedding_margin": wake_margin,
                    "wake_backend": wake_backend,
                    "detected_intent": detected_intent,
                    "detected_path": detected_path,
                    "intent_correct": intent_ok,
                    "stt_exact_match": exact,
                    "latency_ms": latency_ms,
                    "capture_ms": capture_ms,
                    "audio_duration_ms": audio_duration_ms,
                    "transcribe_ms": transcribe_ms,
                    "transcribe_cached": bool(out.get("cached")),
                    "pcm_bytes": len(pcm),
                    "model_load_ms": model_load_ms if len(results) == 0 else None,
                    "failure_reason": failure,
                }
                results.append(row)
                print(
                    f"  heard: {transcript!r} wake={detected_wake} "
                    f"(pcm={wake_embed_ok}/{wake_method} tx={wake_tx_ok}) "
                    f"margin={wake_margin} audio_ms={audio_duration_ms:.0f} "
                    f"intent={detected_intent} ok={intent_ok} tx_ms={transcribe_ms:.0f}"
                )
            except KeyboardInterrupt:
                print("Interrupted — stopping modern live benchmark.")
                return results
            except Exception as e:
                results.append(
                    {
                        "spoken_phrase": spoken,
                        "expected_intent": expected_intent,
                        "expected_path": expected_path,
                        "mode": "modern_live",
                        "trial_index": trial + 1,
                        "voice_variant": "live_mic_modern",
                        "recognized_transcript": "",
                        "normalized_transcript": "",
                        "confidence": None,
                        "wake_detected": False,
                        "wake_embedding_detected": False,
                        "wake_transcript_detected": False,
                        "wake_embedding_margin": None,
                        "wake_backend": modern_info.get("wake_backend"),
                        "detected_intent": None,
                        "detected_path": None,
                        "intent_correct": False,
                        "stt_exact_match": False,
                        "latency_ms": 0.0,
                        "capture_ms": 0.0,
                        "transcribe_ms": 0.0,
                        "pcm_bytes": 0,
                        "failure_reason": f"error:{e}",
                    }
                )
                print(f"  ERROR: {e}")

    return results


def print_comparison_report(
    modern_live_summary: dict[str, Any] | None = None,
    *,
    legacy_skipped: str | None = None,
) -> None:
    """Compare legacy live vs modern synthetic vs modern live from saved JSON logs."""

    def _load_summary(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            s = data.get("summary") or {}
            modes = data.get("modes_run") or []
            # Legacy live-only runs may be buried in combined file — filter trials
            if "modern_live" in modes or data.get("engine") == "aica-voice-v2":
                return s
            if "live" in modes and "modern_live" not in modes:
                live_trials = [t for t in data.get("trials", []) if t.get("mode") == "live"]
                if live_trials:
                    wake = [t for t in live_trials if t.get("expected_intent") == "WAKE"]
                    nav = [t for t in live_trials if t.get("expected_intent") != "WAKE"]
                    return {
                        "wake_accuracy": round(sum(1 for t in wake if t.get("intent_correct")) / len(wake), 3)
                        if wake
                        else None,
                        "intent_accuracy": round(sum(1 for t in nav if t.get("intent_correct")) / len(nav), 3)
                        if nav
                        else None,
                        "stt_exact_match_rate": round(
                            sum(1 for t in live_trials if t.get("stt_exact_match")) / len(live_trials), 3
                        ),
                        "avg_latency_ms": round(
                            sum(float(t.get("latency_ms") or 0) for t in live_trials) / len(live_trials), 1
                        ),
                    }
            return s
        except Exception:
            return None

    base = logs_dir()
    legacy = None if legacy_skipped else (
        _load_summary(base / "voice_benchmark_combined.json")
        or _load_summary(base / "voice_benchmark_baseline.json")
    )
    synthetic = _load_summary(base / "voice_benchmark_modern_final.json") or _load_summary(
        base / "voice_benchmark_modern.json"
    )
    live = modern_live_summary or _load_summary(base / "voice_benchmark_modern_live.json")

    print("\n=== COMPARISON: Legacy live vs Modern synthetic vs Modern live ===")
    if legacy_skipped and not legacy:
        print(f"  Legacy live (System.Speech): SKIPPED ({legacy_skipped})")
    elif legacy:
        print(
            f"  Legacy live (System.Speech): wake={legacy.get('wake_accuracy')} "
            f"intent={legacy.get('intent_accuracy')} "
            f"stt={legacy.get('stt_exact_match_rate')} latency_ms={legacy.get('avg_latency_ms')}"
        )
    else:
        print("  Legacy live (System.Speech): (no data)")

    for label, s in [
        ("Modern synthetic (TTS+Whisper)", synthetic),
        ("Modern live (mic+Whisper)", live),
    ]:
        if not s:
            print(f"  {label}: (no data)")
            continue
        print(
            f"  {label}: wake={s.get('wake_accuracy')} intent={s.get('intent_accuracy')} "
            f"stt={s.get('stt_exact_match_rate')} latency_ms={s.get('avg_latency_ms')}"
        )

    if live:
        print("\n--- Modern live transcripts ---")
        for item in live.get("transcripts") or []:
            mark = "OK" if item.get("ok") else "FAIL"
            print(
                f"  [{mark}] expected={item.get('expected')} heard={item.get('heard')!r} "
                f"detected={item.get('detected')} wake={item.get('wake')}"
            )
        wake_ok = live.get("wake_accuracy")
        intent_ok = live.get("intent_accuracy")
        ready = (
            wake_ok is not None
            and intent_ok is not None
            and wake_ok >= 0.8
            and intent_ok >= 0.8
        )
        print(
            f"\nIS THE MODERN VOICE PIPELINE READY FOR REAL-WORLD DEMO? "
            f"{'YES' if ready else 'NO'}"
        )
        if not ready:
            print("(Threshold: >= 80% wake and >= 80% intent on live mic benchmark)")


def get_recognizer_info() -> dict[str, str]:
    SpeechRecognitionEngine, *_ = load_system_speech()
    eng = SpeechRecognitionEngine()
    info = {
        "engine": "windows-system-speech",
        "recognizer": str(eng.RecognizerInfo.Description),
        "culture": str(eng.RecognizerInfo.Culture),
    }
    eng.Dispose()
    return info


def synthesize_wav(text: str, voice_name: str | None, out_path: Path) -> None:
    import clr  # type: ignore

    clr.AddReference(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\WPF\System.Speech.dll")
    clr.AddReference("System")
    from System.IO import FileStream, FileMode  # type: ignore
    from System.Speech.Synthesis import SpeechSynthesizer  # type: ignore

    synth = SpeechSynthesizer()
    stream = FileStream(str(out_path), FileMode.Create)
    try:
        if voice_name:
            synth.SelectVoice(voice_name)
        synth.SetOutputToWaveStream(stream)
        synth.Speak(text)
    finally:
        synth.SetOutputToNull()
        stream.Close()
        synth.Dispose()


def recognize_wav_file(wav_path: Path) -> tuple[str, float | None, float]:
    SpeechRecognitionEngine, DictationGrammar, _RecognizeMode, _TimeSpan = load_system_speech()
    import clr  # type: ignore

    clr.AddReference("System")
    from System.IO import FileStream, FileMode  # type: ignore

    eng = SpeechRecognitionEngine()
    try:
        eng.SetInputToWaveStream(FileStream(str(wav_path), FileMode.Open))
        eng.LoadGrammar(DictationGrammar())
        t0 = time.perf_counter()
        result = eng.Recognize()
        latency_ms = (time.perf_counter() - t0) * 1000.0
        if result is None:
            return "", None, latency_ms
        text = str(result.Text or "").strip()
        conf = None
        try:
            conf = float(result.Confidence)
        except Exception:
            pass
        return text, conf, latency_ms
    finally:
        eng.Dispose()


def recognize_live_mic(timeout_s: float = 8.0) -> tuple[str, float | None, float]:
    SpeechRecognitionEngine, DictationGrammar, _RecognizeMode, TimeSpan = load_system_speech()
    eng = SpeechRecognitionEngine()
    try:
        eng.SetInputToDefaultAudioDevice()
        eng.LoadGrammar(DictationGrammar())
        eng.InitialSilenceTimeout = TimeSpan.FromSeconds(int(timeout_s))
        eng.BabbleTimeout = TimeSpan.FromSeconds(4)
        eng.EndSilenceTimeout = TimeSpan.FromMilliseconds(900)
        t0 = time.perf_counter()
        result = eng.Recognize()
        latency_ms = (time.perf_counter() - t0) * 1000.0
        if result is None:
            return "", None, latency_ms
        text = str(result.Text or "").strip()
        conf = None
        try:
            conf = float(result.Confidence)
        except Exception:
            pass
        return text, conf, latency_ms
    finally:
        eng.Dispose()


def run_tts_benchmark(trials_per_phrase: int) -> list[TrialResult]:
    voices = ["Microsoft David Desktop", "Microsoft Zira Desktop"]
    results: list[TrialResult] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="aica_voice_bench_"))

    for spoken, expected_intent, expected_path in CORE_PHRASES:
        for trial in range(trials_per_phrase):
            voice = voices[trial % len(voices)]
            wav = tmpdir / f"{abs(hash(spoken))}_{trial}.wav"
            try:
                synthesize_wav(spoken, voice, wav)
                transcript, conf, latency_ms = recognize_wav_file(wav)
            except Exception as e:
                results.append(
                    TrialResult(
                        spoken_phrase=spoken,
                        expected_intent=expected_intent,
                        expected_path=expected_path,
                        mode="tts",
                        trial_index=trial + 1,
                        voice_variant=voice,
                        recognized_transcript="",
                        confidence=None,
                        wake_detected=False,
                        detected_intent=None,
                        detected_path=None,
                        intent_correct=False,
                        stt_exact_match=False,
                        latency_ms=0.0,
                        failure_reason=f"synthesis_or_recognition_error:{e}",
                    )
                )
                continue

            row = evaluate_transcript(spoken, expected_intent, expected_path, transcript)
            row.mode = "tts"
            row.trial_index = trial + 1
            row.voice_variant = voice
            row.confidence = conf
            row.latency_ms = latency_ms
            if not row.failure_reason and not row.stt_exact_match and row.intent_correct:
                row.notes = "intent_ok_transcript_differs"
            results.append(row)

    return results


def run_live_benchmark(trials_per_phrase: int, skip_if_no_input: bool) -> list[TrialResult]:
    results: list[TrialResult] = []
    print("\n=== LIVE MICROPHONE BENCHMARK ===")
    print("Speak each phrase clearly when prompted. Ctrl+C to skip remaining.\n")

    for spoken, expected_intent, expected_path in CORE_PHRASES:
        for trial in range(trials_per_phrase):
            print(f"[trial {trial + 1}/{trials_per_phrase}] Say: \"{spoken}\"")
            try:
                transcript, conf, latency_ms = recognize_live_mic(timeout_s=10.0)
            except KeyboardInterrupt:
                print("Interrupted — stopping live benchmark.")
                return results
            except Exception as e:
                results.append(
                    TrialResult(
                        spoken_phrase=spoken,
                        expected_intent=expected_intent,
                        expected_path=expected_path,
                        mode="live",
                        trial_index=trial + 1,
                        voice_variant="live_mic",
                        recognized_transcript="",
                        confidence=None,
                        wake_detected=False,
                        detected_intent=None,
                        detected_path=None,
                        intent_correct=False,
                        stt_exact_match=False,
                        latency_ms=0.0,
                        failure_reason=f"live_error:{e}",
                    )
                )
                continue

            if skip_if_no_input and not transcript:
                print("  (no speech detected — skipping trial)")
                continue

            row = evaluate_transcript(spoken, expected_intent, expected_path, transcript)
            row.mode = "live"
            row.trial_index = trial + 1
            row.voice_variant = "live_mic"
            row.confidence = conf
            row.latency_ms = latency_ms
            print(f"  heard: {transcript!r} conf={conf} intent_ok={row.intent_correct}")
            results.append(row)

    return results


def analyze_production_log() -> tuple[list[TrialResult], dict[str, Any]]:
    log_path = logs_dir() / "voice.log"
    if not log_path.is_file():
        return [], {"available": False, "reason": "voice.log missing"}

    text = log_path.read_text(encoding="utf-8", errors="replace")
    wake_heard = re.findall(r'wake_heard.*"text": "([^"]+)"', text)
    wake_matched = re.findall(r'wake_matched.*"text": "([^"]+)"', text)
    recognized = re.findall(r'recognized.*"text": "([^"]+)"', text)

    rows: list[TrialResult] = []
    for i, transcript in enumerate(wake_heard, 1):
        row = evaluate_transcript("Hey Ira", "WAKE", None, transcript)
        row.mode = "production_log_wake"
        row.trial_index = i
        row.voice_variant = "ambient_dictation"
        row.notes = "historical ambient wake_heard fragment"
        rows.append(row)

    for i, transcript in enumerate(recognized, 1):
        # Best-effort map to nearest expected phrase
        expected_intent = None
        expected_path = None
        spoken = transcript
        nav = match_nav(transcript)
        if nav:
            expected_intent, expected_path, _ = nav
        row = evaluate_transcript(spoken, expected_intent or "UNKNOWN", expected_path, transcript)
        row.mode = "production_log_command"
        row.trial_index = i
        row.voice_variant = "live_mic_historical"
        row.intent_correct = nav is not None
        row.failure_reason = None if nav else "no_nav_regex_match"
        row.notes = "historical command recognition"
        rows.append(row)

    summary = {
        "available": True,
        "log_path": str(log_path),
        "wake_heard_count": len(wake_heard),
        "wake_matched_count": len(wake_matched),
        "recognized_count": len(recognized),
        "wake_match_rate": round(len(wake_matched) / len(wake_heard), 3) if wake_heard else None,
        "command_nav_match_rate": round(
            sum(1 for t in recognized if match_nav(t)) / len(recognized), 3
        )
        if recognized
        else None,
    }
    return rows, summary


def summarize(trials: list[TrialResult]) -> dict[str, Any]:
    if not trials:
        return {}

    def rate(key: str, filt=None) -> float | None:
        subset = [t for t in trials if filt(t)] if filt else trials
        if not subset:
            return None
        if key == "stt":
            return round(sum(1 for t in subset if t.stt_exact_match) / len(subset), 3)
        if key == "wake":
            wake = [t for t in subset if t.expected_intent == "WAKE"]
            if not wake:
                return None
            return round(sum(1 for t in wake if t.intent_correct) / len(wake), 3)
        if key == "intent":
            nav = [t for t in subset if t.expected_intent != "WAKE"]
            if not nav:
                return None
            return round(sum(1 for t in nav if t.intent_correct) / len(nav), 3)
        return None

    automated = [t for t in trials if t.mode in ("tts", "live", "whisper_tts")]
    latencies = [t.latency_ms for t in automated if t.latency_ms > 0]

    failures = [t for t in trials if not t.intent_correct and t.mode in ("tts", "live", "whisper_tts")]
    successes = [t for t in trials if t.intent_correct and t.mode in ("tts", "live", "whisper_tts")]

    worst = None
    if failures:
        worst = max(
            failures,
            key=lambda t: (0 if t.recognized_transcript else 1, t.spoken_phrase),
        )

    best = None
    if successes:
        best = min(successes, key=lambda t: t.latency_ms)

    return {
        "trial_count": len(trials),
        "automated_trial_count": len(automated),
        "stt_exact_match_rate": rate("stt", lambda t: t.mode in ("tts", "live", "whisper_tts")),
        "wake_accuracy": rate("wake", lambda t: t.mode in ("tts", "live", "whisper_tts")),
        "intent_accuracy": rate("intent", lambda t: t.mode in ("tts", "live", "whisper_tts")),
        "overall_intent_accuracy": rate("intent"),
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "worst_failure": asdict(worst) if worst else None,
        "best_case": asdict(best) if best else None,
        "by_phrase": _by_phrase(automated),
    }


def _by_phrase(trials: list[TrialResult]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for spoken, _, _ in CORE_PHRASES:
        subset = [t for t in trials if t.spoken_phrase == spoken]
        if not subset:
            continue
        out[spoken] = {
            "trials": len(subset),
            "intent_accuracy": round(sum(1 for t in subset if t.intent_correct) / len(subset), 3),
            "stt_exact_match_rate": round(sum(1 for t in subset if t.stt_exact_match) / len(subset), 3),
            "sample_transcripts": list({t.recognized_transcript for t in subset if t.recognized_transcript})[:5],
        }
    return out


def run_whisper_tts_benchmark(trials_per_phrase: int) -> list[TrialResult]:
    """Benchmark faster-whisper + voice_intents (modern stack)."""
    from desktop.launcher.voice_intents import detect_wake, match_intent
    from desktop.launcher.voice_stt import WhisperSTT

    voices = ["Microsoft David Desktop", "Microsoft Zira Desktop"]
    WhisperSTT.get().ensure_loaded()
    tmp = Path(tempfile.mkdtemp())
    synthesize_wav("hello", voices[0], tmp / "warm.wav")
    with wave.open(str(tmp / "warm.wav"), "rb") as wf:
        WhisperSTT.get().transcribe_pcm(wf.readframes(wf.getnframes()))

    results: list[TrialResult] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="aica_voice_modern_"))

    for spoken, expected_intent, expected_path in CORE_PHRASES:
        for trial in range(trials_per_phrase):
            voice = voices[trial % len(voices)]
            wav = tmpdir / f"m_{abs(hash(spoken))}_{trial}.wav"
            try:
                synthesize_wav(spoken, voice, wav)
                with wave.open(str(wav), "rb") as wf:
                    pcm = wf.readframes(wf.getnframes())
                t0 = time.perf_counter()
                out = WhisperSTT.get().transcribe_pcm(pcm)
                latency_ms = (time.perf_counter() - t0) * 1000.0
                transcript = out.get("text") or ""
                conf = out.get("confidence")
            except Exception as e:
                results.append(
                    TrialResult(
                        spoken_phrase=spoken,
                        expected_intent=expected_intent,
                        expected_path=expected_path,
                        mode="whisper_tts",
                        trial_index=trial + 1,
                        voice_variant=voice,
                        recognized_transcript="",
                        confidence=None,
                        wake_detected=False,
                        detected_intent=None,
                        detected_path=None,
                        intent_correct=False,
                        stt_exact_match=False,
                        latency_ms=0.0,
                        failure_reason=str(e),
                    )
                )
                continue

            if expected_intent == "WAKE":
                intent_ok = detect_wake(transcript)
                detected_intent = "WAKE" if intent_ok else None
                detected_path = None
            else:
                m = match_intent(transcript)
                intent_ok = m is not None and m.intent.name == expected_intent
                detected_intent = m.intent.name if m else None
                detected_path = m.intent.path if m else None

            norm_spoken = re.sub(r"\s+", " ", spoken.lower().strip())
            norm_tx = re.sub(r"\s+", " ", transcript.lower().strip())
            exact = norm_tx == norm_spoken

            results.append(
                TrialResult(
                    spoken_phrase=spoken,
                    expected_intent=expected_intent,
                    expected_path=expected_path,
                    mode="whisper_tts",
                    trial_index=trial + 1,
                    voice_variant=voice,
                    recognized_transcript=transcript,
                    confidence=conf,
                    wake_detected=detect_wake(transcript),
                    detected_intent=detected_intent,
                    detected_path=detected_path,
                    intent_correct=intent_ok,
                    stt_exact_match=exact,
                    latency_ms=latency_ms,
                    failure_reason=None if intent_ok else "intent_or_wake_miss",
                )
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark AICA voice stack")
    parser.add_argument(
        "--mode",
        choices=("tts", "live", "log", "all", "whisper"),
        default="all",
        help="live=mic; whisper=modern synthetic TTS path",
    )
    parser.add_argument(
        "--backend",
        choices=("legacy", "modern", "both"),
        default="both",
        help="legacy=System.Speech, modern=faster-whisper+intents+embedding wake",
    )
    parser.add_argument("--trials", type=int, default=3, help="Trials per phrase (tts/live)")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Include interactive live mic benchmark (backend selects legacy vs modern)",
    )
    parser.add_argument("--output", type=str, default="", help="Override output JSON path")
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Print legacy vs modern synthetic vs modern live comparison after run",
    )
    args = parser.parse_args()

    all_trials: list[TrialResult] = []
    modern_live_trials: list[dict[str, Any]] = []
    modes_run: list[str] = []
    log_summary: dict[str, Any] = {}
    modern_live_summary: dict[str, Any] | None = None

    legacy_skip_reason: str | None = None
    legacy_info: dict[str, str] | None = None

    need_legacy_run = args.backend in ("legacy", "both")
    need_legacy_compare = bool(args.compare) and args.backend == "modern"

    if need_legacy_run or need_legacy_compare:
        legacy_info, legacy_skip_reason = try_get_recognizer_info()
        if legacy_skip_reason and need_legacy_run and args.backend == "legacy":
            print(f"FAIL: System.Speech unavailable ({legacy_skip_reason})")
            return 1
        if legacy_skip_reason and (need_legacy_run or need_legacy_compare):
            print(f"Legacy live: SKIPPED ({legacy_skip_reason})")

    if args.backend == "modern":
        mi = get_modern_engine_info()
        report_engine = mi["engine"]
        report_recognizer = mi["recognizer"]
        report_culture = mi["culture"]
    elif legacy_info:
        report_engine = legacy_info["engine"]
        report_recognizer = legacy_info["recognizer"]
        report_culture = legacy_info["culture"]
    else:
        report_engine = "unknown"
        report_recognizer = "unknown"
        report_culture = "unknown"

    if args.backend in ("legacy", "both") and args.mode in ("tts", "all"):
        if legacy_info is None:
            print(f"Legacy TTS benchmark: SKIPPED ({legacy_skip_reason or 'unavailable'})")
        else:
            print("Running TTS->WAV->DictationGrammar benchmark (legacy System.Speech)...")
            all_trials.extend(run_tts_benchmark(args.trials))
            modes_run.append("legacy_tts")

    if args.backend in ("modern", "both") and args.mode in ("whisper", "all"):
        print("Running TTS->WAV->faster-whisper benchmark (modern stack)...")
        all_trials.extend(run_whisper_tts_benchmark(args.trials))
        modes_run.append("whisper_tts")

    run_legacy_live = (args.mode == "live" or args.live) and args.backend in ("legacy", "both")
    run_modern_live = (args.mode == "live" or args.live) and args.backend in ("modern", "both")

    if run_legacy_live:
        if legacy_info is None:
            print(f"Legacy live benchmark: SKIPPED ({legacy_skip_reason or 'unavailable'})")
        else:
            live_rows = run_live_benchmark(args.trials, skip_if_no_input=False)
            all_trials.extend(live_rows)
            modes_run.append("live")

    if run_modern_live:
        modern_live_trials = run_modern_live_benchmark(args.trials)
        modes_run.append("modern_live")
        modern_live_summary = summarize_modern_live(modern_live_trials)
        mi = get_modern_engine_info()
        report_engine = mi["engine"]
        report_recognizer = mi["recognizer"]
        report_culture = mi["culture"]

    if args.mode in ("log", "all"):
        print("Analyzing production voice.log...")
        log_rows, log_summary = analyze_production_log()
        all_trials.extend(log_rows)
        modes_run.append("log")

    # Modern-live-only run: dedicated JSON report
    if modern_live_trials and args.mode == "live" and args.backend == "modern":
        ml_report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "engine": report_engine,
            "recognizer": report_recognizer,
            "culture": report_culture,
            "wake_backend": get_modern_engine_info().get("wake_backend"),
            "modes_run": modes_run,
            "trials": modern_live_trials,
            "summary": modern_live_summary or {},
        }
        ml_out = Path(args.output) if args.output else logs_dir() / "voice_benchmark_modern_live.json"
        ml_out.parent.mkdir(parents=True, exist_ok=True)
        ml_out.write_text(json.dumps(ml_report, indent=2), encoding="utf-8")
        s = modern_live_summary or {}
        print("\n=== MODERN LIVE BENCHMARK COMPLETE ===")
        print(f"Output: {ml_out}")
        print(
            f"Summary: wake={s.get('wake_accuracy')} intent={s.get('intent_accuracy')} "
            f"stt={s.get('stt_exact_match_rate')} avg_latency_ms={s.get('avg_latency_ms')} "
            f"first_tx_ms={s.get('first_transcribe_ms')} subsequent_tx_ms={s.get('subsequent_transcribe_ms')}"
        )
        if args.compare or modern_live_trials:
            print_comparison_report(modern_live_summary, legacy_skipped=legacy_skip_reason)
        return 0

    report = BenchmarkReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        engine=report_engine,
        recognizer=report_recognizer,
        culture=report_culture,
        modes_run=modes_run,
        trials=[asdict(t) for t in all_trials] + modern_live_trials,
        summary=summarize(all_trials) if all_trials else (modern_live_summary or {}),
        production_log_summary=log_summary,
    )

    if args.output:
        out = Path(args.output)
    elif args.backend == "modern" and "modern_live" in modes_run:
        out = logs_dir() / "voice_benchmark_modern_live.json"
    elif args.backend == "modern":
        out = logs_dir() / "voice_benchmark_modern.json"
    elif args.backend == "legacy":
        out = logs_dir() / "voice_benchmark_baseline.json"
    else:
        out = logs_dir() / "voice_benchmark_combined.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")

    s = report.summary
    auto = {k: v for k, v in s.items() if k in (
        "stt_exact_match_rate", "wake_accuracy", "intent_accuracy", "avg_latency_ms"
    )}
    print("\n=== BENCHMARK COMPLETE ===")
    print(f"Output: {out}")
    print(f"Recognizer: {report_recognizer} ({report_culture})")
    print(f"Summary: {json.dumps(auto, indent=2)}")
    if modern_live_summary:
        print(f"Modern live summary: {json.dumps(modern_live_summary, indent=2)}")
    if log_summary.get("available"):
        print(
            "Production log:",
            f"wake_matched {log_summary.get('wake_matched_count')}/{log_summary.get('wake_heard_count')},",
            f"command_nav {log_summary.get('command_nav_match_rate')}",
        )
    if args.compare or "modern_live" in modes_run:
        print_comparison_report(modern_live_summary, legacy_skipped=legacy_skip_reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
