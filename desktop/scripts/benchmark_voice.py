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

Examples:
  python desktop/scripts/benchmark_voice.py
  python desktop/scripts/benchmark_voice.py --mode live --trials 3
  python desktop/scripts/benchmark_voice.py --mode all --trials 2
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
    ("Switch to organization", "OPEN_INTERFACE", "/select-interface"),
    ("Open dashboard", "OPEN_DASHBOARD", "/"),
    ("Open inventory", "OPEN_INVENTORY", "/warehouse"),
    ("Open billing", "OPEN_POS", "/pos"),
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
    from desktop.launcher.voice_bridge import _load_system_speech

    return _load_system_speech()


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

    automated = [t for t in trials if t.mode in ("tts", "live")]
    latencies = [t.latency_ms for t in automated if t.latency_ms > 0]

    failures = [t for t in trials if not t.intent_correct and t.mode in ("tts", "live")]
    successes = [t for t in trials if t.intent_correct and t.mode in ("tts", "live")]

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
        "stt_exact_match_rate": rate("stt", lambda t: t.mode in ("tts", "live")),
        "wake_accuracy": rate("wake", lambda t: t.mode in ("tts", "live")),
        "intent_accuracy": rate("intent", lambda t: t.mode in ("tts", "live")),
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark current AICA System.Speech voice stack")
    parser.add_argument("--mode", choices=("tts", "live", "log", "all"), default="all")
    parser.add_argument("--trials", type=int, default=3, help="Trials per phrase (tts/live)")
    parser.add_argument("--live", action="store_true", help="Include interactive live mic benchmark")
    parser.add_argument("--output", type=str, default="", help="Override output JSON path")
    args = parser.parse_args()

    info = get_recognizer_info()
    all_trials: list[TrialResult] = []
    modes_run: list[str] = []
    log_summary: dict[str, Any] = {}

    if args.mode in ("tts", "all"):
        print("Running TTS->WAV->DictationGrammar benchmark (automated, en-US synthetic speech)...")
        all_trials.extend(run_tts_benchmark(args.trials))
        modes_run.append("tts")

    if args.mode == "live" or args.live:
        live_rows = run_live_benchmark(args.trials, skip_if_no_input=False)
        all_trials.extend(live_rows)
        modes_run.append("live")

    if args.mode in ("log", "all"):
        print("Analyzing production voice.log...")
        log_rows, log_summary = analyze_production_log()
        all_trials.extend(log_rows)
        modes_run.append("log")

    report = BenchmarkReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        engine=info["engine"],
        recognizer=info["recognizer"],
        culture=info["culture"],
        modes_run=modes_run,
        trials=[asdict(t) for t in all_trials],
        summary=summarize(all_trials),
        production_log_summary=log_summary,
    )

    out = Path(args.output) if args.output else output_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")

    s = report.summary
    auto = {k: v for k, v in s.items() if k in (
        "stt_exact_match_rate", "wake_accuracy", "intent_accuracy", "avg_latency_ms"
    )}
    print("\n=== BENCHMARK COMPLETE ===")
    print(f"Output: {out}")
    print(f"Recognizer: {info['recognizer']} ({info['culture']})")
    print(f"Summary: {json.dumps(auto, indent=2)}")
    if log_summary.get("available"):
        print(
            "Production log:",
            f"wake_matched {log_summary.get('wake_matched_count')}/{log_summary.get('wake_heard_count')},",
            f"command_nav {log_summary.get('command_nav_match_rate')}",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
