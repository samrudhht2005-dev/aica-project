"""
Diagnose false wake activations — full scoring breakdown per phrase.

Usage:
  python desktop/scripts/diagnose_false_wake.py --phrase mamacita
  python desktop/scripts/diagnose_false_wake.py --wav path\\to\\clip.wav
  python desktop/scripts/diagnose_false_wake.py --session   # all calibration samples
  python desktop/scripts/diagnose_false_wake.py --hard-negs   # hard-negative folder if present

Does NOT change production behavior.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop.launcher.voice_wake_diagnose import trace_wake_activation
from desktop.scripts.calibrate_wake_voice import calibration_root, record_fixed_seconds, _write_wav


def hard_negatives_root() -> Path:
    try:
        from backend.runtime_paths import appdata_dir

        d = appdata_dir() / "logs" / "wake_hard_negatives"
    except Exception:
        d = Path(os.environ.get("APPDATA") or ".") / "AICA" / "logs" / "wake_hard_negatives"
    d.mkdir(parents=True, exist_ok=True)
    return d


def synth_wav(text: str, path: Path) -> None:
    import clr  # type: ignore

    clr.AddReference(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\WPF\System.Speech.dll")
    from System.IO import FileStream, FileMode  # type: ignore
    from System.Speech.Synthesis import SpeechSynthesizer  # type: ignore

    synth = SpeechSynthesizer()
    stream = FileStream(str(path), FileMode.Create)
    try:
        try:
            synth.SelectVoice("Microsoft Zira Desktop")
        except Exception:
            pass
        synth.SetOutputToWaveStream(stream)
        synth.Speak(text)
    finally:
        synth.SetOutputToNull()
        stream.Close()
        synth.Dispose()


def read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        return wf.readframes(wf.getnframes())


def print_trace(label: str, trace: dict) -> None:
    print("\n" + "=" * 70)
    print(label)
    g = trace["generic"]
    print(
        f"  GENERIC   wake={g['wake_sim']:+.4f} neg={g['neg_sim']:+.4f} "
        f"margin={g['margin']:+.4f}"
    )
    if trace.get("personal"):
        p = trace["personal"]
        print(
            f"  PERSONAL  wake={p['wake_sim']:+.4f} neg={p['neg_sim']:+.4f} "
            f"margin={p['margin']:+.4f} "
            f"(centroid={p.get('centroid_sim')} max_sample={p.get('max_sample_sim')})"
        )
    c = trace["combined"]
    print(
        f"  COMBINED  wake={c['wake_sim']:+.4f} neg={c['neg_sim']:+.4f} "
        f"margin={c['margin']:+.4f} path={c['winning_path']}"
    )
    d = trace["decision"]
    print(f"  THRESHOLD {trace['threshold']}  preprocess={trace['preprocess']}")
    print(f"  WOULD FIRE: {d['would_fire']} via {d['activation_method']}")
    print(f"  REASON: {d['reason']}")
    if d.get("whisper_transcript"):
        print(f"  WHISPER: {d['whisper_transcript']!r} fire={d['whisper_would_fire']}")


HARD_NEG_PROMPTS = [
    ("hard_mamacita", 'Say: "mamacita"'),
    ("hard_mamma_mia", 'Say: "mamma mia"'),
    ("hard_hey_google", 'Say: "Hey Google"'),
    ("hard_hey_siri", 'Say: "Hey Siri"'),
    ("hard_hello_aira", 'Say: "Hello Aira"'),
    ("hard_aira_alone", 'Say only: "Aira"'),
    ("hard_ira_alone", 'Say only: "Ira"'),
    ("hard_erica", 'Say: "Erica"'),
    ("hard_anita", 'Say: "Anita"'),
    ("hard_how_are_you", 'Say: "How are you"'),
    ("hard_weather", 'Say: "What is the weather"'),
    ("hard_open_expenses", 'Say: "Open expenses"'),
    ("hard_switch_pos", 'Say: "Switch to POS"'),
    ("hard_random", 'Say any normal sentence'),
]


def collect_hard_negatives() -> Path:
    session_id = datetime.now().strftime("hardneg_%Y%m%d_%H%M%S")
    session = hard_negatives_root() / session_id
    session.mkdir(parents=True, exist_ok=True)
    rows = []
    print("Hard-negative collection — YOUR mic, confusing / false-trigger phrases")
    print("Session:", session)
    input("Press Enter to begin… ")
    for i, (key, prompt) in enumerate(HARD_NEG_PROMPTS, 1):
        print(f"\n--- Hard negative {i}/{len(HARD_NEG_PROMPTS)} ---")
        print(prompt)
        custom = input("Press Enter to record, or type a custom phrase first: ").strip()
        if custom:
            prompt = custom
            key = f"hard_custom_{i:02d}"
        input("Press Enter, then speak (2.5s)… ")
        pcm = record_fixed_seconds(2.5)
        wav_path = session / f"{key}.wav"
        _write_wav(wav_path, pcm)
        trace = trace_wake_activation(pcm, run_whisper=True)
        row = {"label": key, "prompt": prompt, "file": str(wav_path), "trace": trace}
        rows.append(row)
        print_trace(key, trace)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_dir": str(session),
        "samples": rows,
    }
    out = session / "report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    (hard_negatives_root() / "latest_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("\nWrote", out)
    return session


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose false wake activations")
    parser.add_argument("--phrase", action="append", help="TTS phrase to score (synthetic)")
    parser.add_argument("--wav", type=Path, action="append", help="WAV file to trace")
    parser.add_argument("--session", action="store_true", help="Trace original calibration session")
    parser.add_argument("--hard-negs", action="store_true", help="Trace latest hard-negative session")
    parser.add_argument("--collect-hard-negs", action="store_true", help="Interactive hard-negative recording")
    parser.add_argument("--no-whisper", action="store_true", help="Skip Whisper verify step")
    args = parser.parse_args()

    if args.collect_hard_negs:
        collect_hard_negatives()
        return 0

    traces: list[dict] = []

    if args.session:
        latest = calibration_root() / "latest_report.json"
        if latest.is_file():
            session = Path(json.loads(latest.read_text())["session_dir"])
        else:
            sessions = sorted(calibration_root().glob("session_*"), key=lambda p: p.stat().st_mtime, reverse=True)
            session = sessions[0]
        for sub, kind in (("positive", "positive"), ("negative", "negative")):
            for wav in sorted((session / sub).glob("*.wav")):
                pcm = read_pcm(wav)
                t = trace_wake_activation(pcm, run_whisper=not args.no_whisper)
                t["label"] = wav.stem
                t["kind"] = kind
                traces.append(t)
                print_trace(f"{kind} {wav.stem}", t)

    if args.hard_negs:
        latest = hard_negatives_root() / "latest_report.json"
        if latest.is_file():
            session = Path(json.loads(latest.read_text())["session_dir"])
            for wav in sorted(session.glob("*.wav")):
                pcm = read_pcm(wav)
                t = trace_wake_activation(pcm, run_whisper=not args.no_whisper)
                t["label"] = wav.stem
                t["kind"] = "hard_negative"
                traces.append(t)
                print_trace(wav.stem, t)
        else:
            print("No hard-negative session yet. Run: --collect-hard-negs")

    for wav in args.wav or []:
        pcm = read_pcm(wav)
        t = trace_wake_activation(pcm, run_whisper=not args.no_whisper)
        t["label"] = wav.stem
        traces.append(t)
        print_trace(wav.stem, t)

    if args.phrase:
        tmp = Path(tempfile.mkdtemp(prefix="wake_diag_"))
        for phrase in args.phrase:
            wav = tmp / f"{phrase.replace(' ', '_')}.wav"
            synth_wav(phrase, wav)
            pcm = read_pcm(wav)
            t = trace_wake_activation(pcm, run_whisper=not args.no_whisper)
            t["label"] = phrase
            t["kind"] = "tts_synthetic"
            t["note"] = "TTS synthetic — re-record with your mic for definitive results"
            traces.append(t)
            print_trace(f"TTS {phrase!r}", t)

    if not traces and not args.collect_hard_negs:
        parser.print_help()
        return 0

    if traces:
        out = calibration_root() / f"false_wake_diag_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(json.dumps(traces, indent=2), encoding="utf-8")
        print("\nWrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
