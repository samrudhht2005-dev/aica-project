"""
Interactive Hey IRA wake calibration — collect YOUR mic samples and score them
against the CURRENT production embedding detector. Does NOT change thresholds
or embeddings unless you later approve a separate change.

Usage (from repo root, with venv activated):

  python desktop/scripts/calibrate_wake_voice.py --calibrate

  # Re-score an existing session folder:
  python desktop/scripts/calibrate_wake_voice.py --score-session "%APPDATA%\\AICA\\logs\\wake_calibration\\session_..."

Samples are stored ONLY under:
  %APPDATA%\\AICA\\logs\\wake_calibration\\<session_id>\\
    positive\\*.wav
    negative\\*.wav
    report.json

Raw WAVs are gitignored and never packaged into AICA.exe.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop.launcher.voice_audio import FRAME_SAMPLES, SAMPLE_RATE, VoiceActivityDetector
from desktop.launcher.voice_wake_verify import AMBIGUOUS_MARGIN_LOW, STRONG_MARGIN_DEFAULT

# OWW mel+embedding needs ~800 ms of audio (76-frame window @ 8-frame hop).
MIN_EMBED_SAMPLES = int(0.80 * SAMPLE_RATE)
MIN_EMBED_DURATION_S = MIN_EMBED_SAMPLES / SAMPLE_RATE

POSITIVE_PROMPTS: list[tuple[str, str]] = [
    ("pos_01_hey_ira_natural", 'Say naturally: "Hey IRA"'),
    ("pos_02_hey_ira_natural", 'Say naturally: "Hey IRA"'),
    ("pos_03_hey_ira_natural", 'Say naturally: "Hey Ira"'),
    ("pos_04_hey_ira_comma", 'Say: "Hey, IRA"'),
    ("pos_05_hey_ira_normal", 'Say at your normal pace: "Hey IRA"'),
    ("pos_06_hey_ira_slightly_fast", 'Say a bit faster than usual: "Hey IRA"'),
    ("pos_07_hey_ira_slightly_slow", 'Say a bit slower / clearer: "Hey IRA"'),
    ("pos_08_hey_ira_natural", 'Say naturally again: "Hey IRA"'),
    ("pos_09_hey_ira_natural", 'Say naturally: "Hey IRA"'),
    ("pos_10_hey_ira_natural", 'One more natural: "Hey IRA"'),
    ("pos_11_hey_ira_soft", 'Say a little softer (still clear): "Hey IRA"'),
    ("pos_12_hey_ira_clear", 'Say clearly once more: "Hey IRA"'),
]

HI_POSITIVE_PROMPTS: list[tuple[str, str]] = [
    ("pos_hi_01_natural", 'Say naturally: "Hi Aira"'),
    ("pos_hi_02_normal", 'Say at your normal pace: "Hi Aira"'),
    ("pos_hi_03_slightly_fast", 'Say a bit faster than usual: "Hi Aira"'),
    ("pos_hi_04_clear", 'Say clearly: "Hi Aira"'),
]

NEGATIVE_PROMPTS: list[tuple[str, str]] = [
    ("neg_01_hello_there", 'Say: "Hello there"'),
    ("neg_02_hey_google", 'Say: "Hey Google"'),
    ("neg_03_hey_siri", 'Say: "Hey Siri"'),
    ("neg_04_weather", 'Say: "What is the weather"'),
    ("neg_05_open_expenses", 'Say: "Open expenses"'),
    ("neg_06_switch_pos", 'Say: "Switch to POS"'),
    ("neg_07_thank_you", 'Say: "Thank you"'),
    ("neg_08_how_are_you", 'Say: "How are you"'),
    ("neg_09_random", 'Say any normal sentence, e.g. "I need to finish this report"'),
    ("neg_10_random2", 'Say another normal sentence, e.g. "Can you pass the water"'),
    ("neg_11_hey_alone", 'Say only: "Hey"'),
    ("neg_12_ira_alone", 'Say only: "IRA"'),
    ("neg_13_silence", "Stay silent for the recording (room quiet)"),
    ("neg_14_noise", "Do not speak — just room/background noise"),
]


def calibration_root() -> Path:
    try:
        from backend.runtime_paths import appdata_dir

        d = appdata_dir() / "logs" / "wake_calibration"
    except Exception:
        d = Path(os.environ.get("APPDATA") or ".") / "AICA" / "logs" / "wake_calibration"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_wav(path: Path, pcm: bytes, rate: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)


def record_fixed_seconds(seconds: float = 2.5) -> bytes:
    """Record mono 16 kHz int16 via sounddevice (same rate as Desktop MicCapture)."""
    import sounddevice as sd

    n = int(seconds * SAMPLE_RATE)
    # Prefer int16 path; fall back to float32→int16 like MicCapture.stream_frames.
    try:
        audio = sd.rec(n, samplerate=SAMPLE_RATE, channels=1, dtype="int16")
        sd.wait()
        return np.asarray(audio, dtype=np.int16).reshape(-1).tobytes()
    except Exception:
        audio = sd.rec(n, samplerate=SAMPLE_RATE, channels=1, dtype="float32")
        sd.wait()
        pcm = (np.asarray(audio)[:, 0] * 32767.0).astype(np.int16)
        return pcm.tobytes()


def pcm_duration_s(pcm: bytes) -> float:
    return len(pcm) / (SAMPLE_RATE * 2)


def pcm_rms(pcm: bytes) -> float:
    if not pcm or len(pcm) < 4:
        return 0.0
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(arr * arr)))


def can_embed_pcm(pcm: bytes, embedder: Any | None = None) -> bool:
    """True when OWW embed_audio would succeed (no exception)."""
    if not pcm or len(pcm) < MIN_EMBED_SAMPLES * 2:
        return False
    from desktop.launcher.voice_wake_util import OwwOnnxEmbedder

    emb = embedder or OwwOnnxEmbedder()
    audio = np.frombuffer(pcm, dtype=np.int16)
    try:
        emb.embed_audio(audio)
        return True
    except ValueError:
        return False


def _pad_pcm_center(pcm: bytes, min_samples: int) -> bytes:
    arr = np.frombuffer(pcm, dtype=np.int16)
    if arr.size >= min_samples:
        return pcm
    out = np.zeros(min_samples, dtype=np.int16)
    start = (min_samples - arr.size) // 2
    out[start : start + arr.size] = arr
    return out.tobytes()


def extract_vad_speech_segment(pcm: bytes) -> tuple[bytes, bool]:
    """
    Production-like VAD segment from a fixed recording (EmbeddingWakeDetector logic).
    Returns (segment_bytes, found_any_speech).
    """
    if not pcm:
        return pcm, False
    vad = VoiceActivityDetector(aggressiveness=2)
    frame_bytes = FRAME_SAMPLES * 2
    min_speech_s = 0.28
    max_speech_s = 2.2
    silence_limit = 10

    buf = bytearray()
    in_speech = False
    speech_started = 0.0
    silence_frames = 0
    best = b""
    found_speech = False

    t = 0.0
    for i in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
        frame = pcm[i : i + frame_bytes]
        t = i / (SAMPLE_RATE * 2)
        if vad.is_speech(frame):
            found_speech = True
            if not in_speech:
                in_speech = True
                speech_started = t
                buf.clear()
                silence_frames = 0
            buf.extend(frame)
            silence_frames = 0
            if t - speech_started >= max_speech_s:
                best = bytes(buf)
                break
        elif in_speech:
            buf.extend(frame)
            silence_frames += 1
            if silence_frames >= silence_limit:
                if t - speech_started >= min_speech_s:
                    best = bytes(buf)
                break

    if not best and len(buf) >= int(min_speech_s * SAMPLE_RATE) * 2:
        best = bytes(buf)
    if best:
        return best, found_speech
    return pcm, found_speech


def prepare_scoring_pcm(pcm: bytes, embedder: Any | None = None) -> tuple[bytes, dict[str, Any]]:
    """
    Choose PCM for embedding score.

    Prefer production VAD trim when embeddable; otherwise fall back to full clip
    or speech-centered padding so natural short phrases like "Hey IRA" still score.
    """
    trimmed, found_speech = extract_vad_speech_segment(pcm)
    meta: dict[str, Any] = {
        "raw_duration_s": round(pcm_duration_s(pcm), 3),
        "raw_samples": len(pcm) // 2,
        "raw_rms": round(pcm_rms(pcm), 1),
        "vad_trim_duration_s": round(pcm_duration_s(trimmed), 3),
        "vad_found_speech": found_speech,
        "production_vad_scorable": can_embed_pcm(trimmed, embedder) if found_speech else False,
        "min_embed_duration_s": MIN_EMBED_DURATION_S,
        "preprocess": "unknown",
        "scored_duration_s": None,
        "embed_ok": False,
        "fallback_reason": None,
    }

    candidates: list[tuple[str, bytes, str | None]] = []
    if found_speech and trimmed:
        candidates.append(("vad_trim", trimmed, None))
    # Prefer full recording before padded VAD — closer to honest diagnostic scoring.
    candidates.append(("full_clip", pcm, "vad_trim below embedding minimum or no speech"))
    if found_speech and trimmed:
        candidates.append(
            (
                "vad_padded_center",
                _pad_pcm_center(trimmed, MIN_EMBED_SAMPLES),
                "vad_trim below embedding minimum; full clip also failed",
            )
        )

    for strategy, candidate, fallback_reason in candidates:
        if can_embed_pcm(candidate, embedder):
            meta["preprocess"] = strategy
            meta["scored_duration_s"] = round(pcm_duration_s(candidate), 3)
            meta["embed_ok"] = True
            if fallback_reason and strategy != "vad_trim":
                meta["fallback_reason"] = fallback_reason
            return candidate, meta

    meta["preprocess"] = "unscorable"
    meta["scored_duration_s"] = meta["raw_duration_s"]
    meta["fallback_reason"] = "no candidate met embedding minimum length"
    return pcm, meta


def vad_trim_like_production(pcm: bytes) -> bytes:
    """Legacy alias — returns VAD segment only (may be too short to embed)."""
    segment, _ = extract_vad_speech_segment(pcm)
    return segment


def score_pcm(pcm: bytes, *, label: str, path: str, kind: str) -> dict[str, Any]:
    from desktop.launcher.voice_wake_embed import EmbeddingWakeDetector
    from desktop.launcher.voice_wake_util import OwwOnnxEmbedder

    det = EmbeddingWakeDetector()
    det.ensure_loaded()
    embedder = det._embedder or OwwOnnxEmbedder()
    scored_pcm, prep = prepare_scoring_pcm(pcm, embedder)
    thr = float(det._margin_threshold or STRONG_MARGIN_DEFAULT)

    base = {
        "label": label,
        "kind": kind,
        "file": path,
        "raw_duration_s": prep["raw_duration_s"],
        "raw_samples": prep["raw_samples"],
        "raw_rms": prep["raw_rms"],
        "vad_trim_duration_s": prep["vad_trim_duration_s"],
        "vad_found_speech": prep["vad_found_speech"],
        "production_vad_scorable": prep["production_vad_scorable"],
        "scored_duration_s": prep["scored_duration_s"],
        "preprocess": prep["preprocess"],
        "fallback_reason": prep.get("fallback_reason"),
        "min_embed_duration_s": prep["min_embed_duration_s"],
        "threshold": thr,
        "ambiguous_low": AMBIGUOUS_MARGIN_LOW,
        "scorable": prep["embed_ok"],
    }

    if not prep["embed_ok"]:
        return {
            **base,
            "wake_sim": None,
            "neg_sim": None,
            "margin": None,
            "decision": "UNSCORABLE",
            "error": "Audio too short for embedding after VAD trim and fallbacks",
        }

    try:
        wake_sim, neg_sim, margin = det._score_pcm(scored_pcm)
    except ValueError as exc:
        return {
            **base,
            "wake_sim": None,
            "neg_sim": None,
            "margin": None,
            "decision": "UNSCORABLE",
            "error": str(exc),
        }

    if margin >= thr:
        decision = "STRONG_FIRE"
    elif margin >= AMBIGUOUS_MARGIN_LOW:
        decision = "AMBIGUOUS_WHISPER_VERIFY"
    else:
        decision = "REJECT"
    return {
        **base,
        "wake_sim": round(float(wake_sim), 4),
        "neg_sim": round(float(neg_sim), 4),
        "margin": round(float(margin), 4),
        "decision": decision,
        "error": None,
    }


def _stats(margins: list[float]) -> dict[str, Any]:
    if not margins:
        return {"n": 0}
    arr = np.asarray(margins, dtype=np.float64)
    return {
        "n": int(arr.size),
        "mean": round(float(arr.mean()), 4),
        "median": round(float(np.median(arr)), 4),
        "min": round(float(arr.min()), 4),
        "max": round(float(arr.max()), 4),
        "p10": round(float(np.percentile(arr, 10)), 4),
        "p90": round(float(np.percentile(arr, 90)), 4),
    }


def analyze(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scorable = [r for r in rows if r.get("scorable", True) and r.get("margin") is not None]
    unscorable = [r for r in rows if not r.get("scorable", True) or r.get("margin") is None]
    pos = [r for r in scorable if r.get("kind") == "positive"]
    neg = [r for r in scorable if r.get("kind") == "negative"]
    pos_m = [float(r["margin"]) for r in pos]
    neg_m = [float(r["margin"]) for r in neg]
    thr = float(scorable[0]["threshold"]) if scorable else STRONG_MARGIN_DEFAULT

    pos_stats = _stats(pos_m)
    neg_stats = _stats(neg_m)

    overlap = None
    if pos_m and neg_m:
        overlap = {
            "pos_min": pos_stats["min"],
            "neg_max": neg_stats["max"],
            "gap_pos_min_minus_neg_max": round(pos_stats["min"] - neg_stats["max"], 4),
            "distributions_overlap": pos_stats["min"] < neg_stats["max"],
        }

    # Threshold band that fires all positives and rejects all negatives (if separable).
    recommended: dict[str, Any]
    if pos_m and neg_m and pos_stats["min"] > neg_stats["max"]:
        low = neg_stats["max"]
        high = pos_stats["min"]
        mid = (low + high) / 2.0
        # Stay below pos p10 for safety margin on weaker positives
        safe = min(mid, pos_stats["p10"] - 0.005)
        recommended = {
            "separable": True,
            "suggested_threshold_range": [round(low + 0.005, 4), round(high - 0.005, 4)],
            "suggested_threshold": round(max(low + 0.01, min(safe, high - 0.01)), 4),
            "current_threshold": thr,
            "current_would_fire_positives": sum(1 for m in pos_m if m >= thr),
            "current_would_fire_negatives": sum(1 for m in neg_m if m >= thr),
        }
        option = "D" if thr < pos_stats["min"] and thr > neg_stats["max"] else "A"
        # If current thr already separates, D; if needs adjust within band, still D;
        # if positives all below thr due to TTS domain gap → B/C
        if pos_stats["max"] < thr:
            option = "B"
            note = (
                "All your positive margins are below the current +0.02 strong-fire threshold. "
                "Threshold-only lowering is risky unless negatives stay well below. "
                "Prefer adding a personal-voice wake centroid (option B) or rebuilding "
                "references with your samples mixed in (option C)."
            )
        elif recommended["current_would_fire_negatives"] > 0:
            option = "B"
            note = (
                "Current threshold would fire some negatives. Do not lower further. "
                "Personal references (B) or rebuild (C) needed for better separation."
            )
        elif recommended["current_would_fire_positives"] == len(pos_m):
            option = "D"
            note = (
                "Current threshold already separates this session. "
                "Collect another day of samples before changing production."
            )
        else:
            option = "A"
            note = (
                "Distributions are separable; calibration of threshold within the "
                "suggested range may help, but prefer verifying with more sessions first."
            )
    else:
        # Not cleanly separable
        recommended = {
            "separable": False,
            "suggested_threshold_range": None,
            "suggested_threshold": None,
            "current_threshold": thr,
            "current_would_fire_positives": sum(1 for m in pos_m if m >= thr) if pos_m else 0,
            "current_would_fire_negatives": sum(1 for m in neg_m if m >= thr) if neg_m else 0,
        }
        if pos_m and neg_m and pos_stats["mean"] <= neg_stats["mean"]:
            option = "C"
            note = (
                "Your live positives are not scoring above negatives with TTS-built "
                "centroids — classic domain mismatch. Rebuild references using both "
                "general/TTS and your mic samples (option C), or add a personal centroid (B)."
            )
        else:
            option = "B"
            note = (
                "Positive/negative margin distributions overlap. "
                "Do not only lower the threshold. Add personal wake references (B) "
                "or rebuild embeddings with your samples (C)."
            )

    return {
        "positive_stats": pos_stats,
        "negative_stats": neg_stats,
        "overlap": overlap,
        "recommended_threshold_analysis": recommended,
        "calibration_option": option,
        "calibration_rationale": note,
        "unscorable_count": len(unscorable),
        "unscorable_labels": [r.get("label") for r in unscorable],
        "options_legend": {
            "A": "Calibrate using current embedding references (threshold band only)",
            "B": "Add a small personal voice centroid/reference from your samples",
            "C": "Rebuild references using both TTS/general + your mic samples",
            "D": "Threshold adjustment alone is sufficient",
        },
    }


def print_summary(analysis: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    print("\n======== WAKE CALIBRATION SUMMARY ========")
    print("CURRENT threshold (strong fire):", STRONG_MARGIN_DEFAULT)
    print("Ambiguous Whisper band: [", AMBIGUOUS_MARGIN_LOW, ",", STRONG_MARGIN_DEFAULT, ")")
    print("\nPOSITIVES:")
    print(json.dumps(analysis["positive_stats"], indent=2))
    print("\nNEGATIVES:")
    print(json.dumps(analysis["negative_stats"], indent=2))
    print("\nOVERLAP:")
    print(json.dumps(analysis["overlap"], indent=2))
    print("\nTHRESHOLD ANALYSIS:")
    print(json.dumps(analysis["recommended_threshold_analysis"], indent=2))
    print("\nRECOMMENDED OPTION:", analysis["calibration_option"])
    print(analysis["calibration_rationale"])
    print("\nPer-sample margins:")
    for r in rows:
        if r.get("margin") is None:
            print(
                f"  [{r['kind'][:3]}] {r['label']:<32} "
                f"UNSCORABLE ({r.get('error') or 'no margin'}) "
                f"raw={r.get('raw_duration_s')}s vad={r.get('vad_trim_duration_s')}s"
            )
            continue
        fb = f" [{r['preprocess']}]" if r.get("preprocess") else ""
        print(
            f"  [{r['kind'][:3]}] {r['label']:<32} "
            f"wake={r['wake_sim']:+.3f} neg={r['neg_sim']:+.3f} "
            f"margin={r['margin']:+.4f} → {r['decision']}{fb}"
        )


def _prompt_record(
    session: Path,
    key: str,
    prompt: str,
    kind: str,
    seconds: float,
    *,
    allow_retry: bool = True,
) -> dict[str, Any]:
    sub = session / kind
    sub.mkdir(parents=True, exist_ok=True)
    wav_path = sub / f"{key}.wav"

    while True:
        print("\n" + "=" * 60)
        print(f"{kind.upper()}  |  {key}")
        print(prompt)
        if wav_path.is_file() and allow_retry:
            print(f"(Existing recording: {wav_path.name} — Enter to re-record, or type s to score existing)")
            choice = input(f"Press Enter to record ({seconds:.1f}s), or 's' to score existing… ").strip().lower()
            if choice == "s":
                with wave.open(str(wav_path), "rb") as wf:
                    pcm = wf.readframes(wf.getnframes())
                row = score_pcm(pcm, label=key, path=str(wav_path), kind=kind)
                _print_score_row(row)
                if row.get("scorable", True) or not allow_retry:
                    return row
                retry = input("Sample unscorable — re-record? [Y/n] ").strip().lower()
                if retry in ("n", "no"):
                    return row
                continue
        else:
            input(f"Press Enter, then speak (recording {seconds:.1f}s)… ")

        print("● Recording…")
        pcm = record_fixed_seconds(seconds)
        _write_wav(wav_path, pcm)
        print("■ Saved", wav_path.name)
        row = score_pcm(pcm, label=key, path=str(wav_path), kind=kind)
        _print_score_row(row)
        if row.get("scorable", True) or not allow_retry:
            return row
        print("\n⚠ Sample could not be scored:", row.get("error"))
        retry = input("Retry this sample? [Y/n] ").strip().lower()
        if retry in ("n", "no"):
            return row
        time.sleep(0.25)


def _print_score_row(row: dict[str, Any]) -> None:
    if not row.get("scorable", True):
        print(
            f"  raw={row.get('raw_duration_s')}s rms={row.get('raw_rms')} "
            f"vad={row.get('vad_trim_duration_s')}s → UNSCORABLE"
        )
        if row.get("fallback_reason"):
            print(f"  reason: {row['fallback_reason']}")
        return
    fb = ""
    if row.get("preprocess") != "vad_trim":
        fb = f" (scored via {row.get('preprocess')}"
        if row.get("fallback_reason"):
            fb += f": {row['fallback_reason']}"
        fb += ")"
    print(
        f"  raw={row.get('raw_duration_s')}s vad={row.get('vad_trim_duration_s')}s "
        f"scored={row.get('scored_duration_s')}s  "
        f"wake={row['wake_sim']:+.3f} neg={row['neg_sim']:+.3f} "
        f"margin={row['margin']:+.4f} → {row['decision']}{fb}"
    )
    time.sleep(0.25)


def _load_existing_rows(session: Path) -> dict[str, dict[str, Any]]:
    report_path = session / "report.json"
    if not report_path.is_file():
        return {}
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        return {r["label"]: r for r in data.get("samples", []) if r.get("label")}
    except Exception:
        return {}


def _find_resume_session(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit.resolve() if explicit.is_dir() else None
    sessions = sorted(
        calibration_root().glob("session_*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return sessions[0] if sessions else None


def _collect_prompts(
    session: Path,
    prompts: list[tuple[str, str]],
    kind: str,
    seconds: float,
    existing: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    n = len(prompts)
    for i, (key, prompt) in enumerate(prompts, 1):
        wav_path = session / kind / f"{key}.wav"
        if wav_path.is_file() and key in existing and existing[key].get("scorable", True):
            print(f"\n--- {kind.title()} sample {i}/{n} — already recorded: {key} ---")
            rows.append(existing[key])
            _print_score_row(existing[key])
            continue
        if wav_path.is_file():
            print(f"\n--- {kind.title()} sample {i}/{n} — resuming: {key} (WAV on disk) ---")
            with wave.open(str(wav_path), "rb") as wf:
                pcm = wf.readframes(wf.getnframes())
            row = score_pcm(pcm, label=key, path=str(wav_path), kind=kind)
            if row.get("scorable", True):
                rows.append(row)
                _print_score_row(row)
                continue
            print("Existing WAV was unscorable; re-prompting…")
        print(f"\n--- {kind.title()} sample {i}/{n} ---")
        rows.append(_prompt_record(session, key, prompt, kind, seconds))
    return rows


def run_calibrate(*, seconds: float = 2.5, resume_session: Path | None = None) -> Path:
    import sounddevice as sd

    mic = sd.query_devices(kind="input")
    session = _find_resume_session(resume_session)
    resuming = session is not None
    if not resuming:
        session_id = datetime.now().strftime("session_%Y%m%d_%H%M%S")
        session = calibration_root() / session_id
        (session / "positive").mkdir(parents=True, exist_ok=True)
        (session / "negative").mkdir(parents=True, exist_ok=True)
    else:
        session_id = session.name
        (session / "positive").mkdir(parents=True, exist_ok=True)
        (session / "negative").mkdir(parents=True, exist_ok=True)

    existing = _load_existing_rows(session)

    print("AICA Hey IRA — interactive wake calibration")
    print("Mic:", mic.get("name"), "| rate target:", SAMPLE_RATE)
    print("Session:", session)
    if resuming:
        print("Resuming existing session (already-recorded WAVs are kept).")
    print("Production embeddings/threshold are NOT modified.")
    print(
        "Scoring: production VAD trim when embeddable (≥"
        f"{MIN_EMBED_DURATION_S:.2f}s); else full clip or padded fallback."
    )
    if not resuming:
        input("\nPress Enter to begin POSITIVE samples… ")
    else:
        print("\nContinuing from last incomplete sample…")

    rows: list[dict[str, Any]] = []
    rows.extend(_collect_prompts(session, POSITIVE_PROMPTS, "positive", seconds, existing))

    if not any((session / "negative").glob("*.wav")):
        input("\nPress Enter to begin NEGATIVE / non-wake samples… ")
    rows_neg_existing = {k: v for k, v in existing.items() if k.startswith("neg_")}
    # Rebuild negative rows from collected positives + negatives below
    neg_rows = _collect_prompts(session, NEGATIVE_PROMPTS, "negative", seconds, rows_neg_existing)
    # Merge: keep positive rows from first pass, add negatives
    pos_labels = {r["label"] for r in rows}
    rows = [r for r in rows if r["label"] in pos_labels]
    rows.extend(neg_rows)

    analysis = analyze(rows)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "session_dir": str(session),
        "microphone": str(mic.get("name")),
        "sample_rate": SAMPLE_RATE,
        "record_seconds": seconds,
        "production_untouched": True,
        "scoring": {
            "detector": "EmbeddingWakeDetector (production references)",
            "vad_trim": "aggressiveness=2, ~0.28–2.2s (matches Desktop wake when embeddable)",
            "min_embed_duration_s": MIN_EMBED_DURATION_S,
            "fallback": "full_clip or vad_padded_center when VAD segment < embedding minimum",
            "formula": "margin = cos(q, wake_centroid) - cos(q, neg_centroid)",
            "strong_threshold": STRONG_MARGIN_DEFAULT,
            "ambiguous_low": AMBIGUOUS_MARGIN_LOW,
        },
        "samples": rows,
        "analysis": analysis,
    }
    out = session / "report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    # Convenience copy under logs root
    latest = calibration_root() / "latest_report.json"
    latest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print_summary(analysis, rows)
    print("\nWrote", out)
    print("Also", latest)
    return session


def _default_hey_session() -> Path:
    """Original Hey Aira calibration session (12 samples)."""
    latest = calibration_root() / "latest_report.json"
    if latest.is_file():
        data = json.loads(latest.read_text(encoding="utf-8"))
        session = Path(data["session_dir"])
        if session.is_dir() and any((session / "positive").glob("pos_*_hey_*.wav")):
            return session
    sessions = sorted(calibration_root().glob("session_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for s in sessions:
        if any((s / "positive").glob("pos_*_hey_*.wav")):
            return s
    raise SystemExit(
        "No Hey Aira calibration session found. Run --calibrate first or pass --session."
    )


def run_collect_hi_aira(*, seconds: float = 2.5, session: Path | None = None) -> Path:
    """
    Record 4 Hi Aira positives into an existing Hey Aira session.
    Does NOT modify or overwrite the 12 Hey Aira WAV files.
    """
    import sounddevice as sd

    target = (session or _default_hey_session()).resolve()
    pos_dir = target / "positive"
    if not pos_dir.is_dir():
        raise SystemExit(f"Missing positive folder: {pos_dir}")
    hey_count = len(list(pos_dir.glob("pos_*_hey_*.wav")))
    if hey_count < 1:
        print("Warning: no Hey Aira samples found in", pos_dir)

    (target / "positive").mkdir(parents=True, exist_ok=True)
    existing = _load_existing_rows(target)

    mic = sd.query_devices(kind="input")
    print("AICA Hi Aira — supplementary positive calibration")
    print("Mic:", mic.get("name"), "| rate target:", SAMPLE_RATE)
    print("Session:", target)
    print(f"Existing Hey Aira samples in session: {hey_count} (will NOT be modified)")
    print("Adding 4 Hi Aira samples to positive/")
    input("\nPress Enter to begin Hi Aira recordings… ")

    rows: list[dict[str, Any]] = []
    rows.extend(_collect_prompts(target, HI_POSITIVE_PROMPTS, "positive", seconds, existing))

    # Merge with prior report samples (keep Hey + negatives, replace/add Hi rows)
    prior: dict[str, dict[str, Any]] = existing
    for r in rows:
        prior[r["label"]] = r
    all_rows = list(prior.values())

    analysis = analyze(all_rows)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_id": target.name,
        "session_dir": str(target),
        "microphone": str(mic.get("name")),
        "sample_rate": SAMPLE_RATE,
        "record_seconds": seconds,
        "production_untouched": True,
        "hi_aira_extension": True,
        "hey_aira_count": hey_count,
        "hi_aira_count": len(list(pos_dir.glob("pos_hi_*.wav"))),
        "scoring": {
            "detector": "EmbeddingWakeDetector (production references)",
            "vad_trim": "aggressiveness=2, ~0.28–2.2s (matches Desktop wake when embeddable)",
            "min_embed_duration_s": MIN_EMBED_DURATION_S,
            "fallback": "full_clip or vad_padded_center when VAD segment < embedding minimum",
            "formula": "margin = cos(q, wake_centroid) - cos(q, neg_centroid)",
            "strong_threshold": STRONG_MARGIN_DEFAULT,
            "ambiguous_low": AMBIGUOUS_MARGIN_LOW,
        },
        "samples": all_rows,
        "analysis": analysis,
    }
    out = target / "report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    (calibration_root() / "latest_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print_summary(analysis, all_rows)
    print("\nWrote", out)
    print(f"Hi Aira samples: {report['hi_aira_count']}  |  Hey Aira samples: {report['hey_aira_count']}")
    return target


def score_session(session: Path) -> Path:
    session = session.resolve()
    rows: list[dict[str, Any]] = []
    for kind in ("positive", "negative"):
        d = session / kind
        if not d.is_dir():
            continue
        for wav in sorted(d.glob("*.wav")):
            with wave.open(str(wav), "rb") as wf:
                pcm = wf.readframes(wf.getnframes())
            rows.append(score_pcm(pcm, label=wav.stem, path=str(wav), kind=kind))
    analysis = analyze(rows)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_dir": str(session),
        "rescored": True,
        "production_untouched": True,
        "samples": rows,
        "analysis": analysis,
    }
    out = session / "report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    (calibration_root() / "latest_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print_summary(analysis, rows)
    print("Wrote", out)
    return session


def main() -> int:
    parser = argparse.ArgumentParser(description="Hey IRA interactive wake calibration")
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Interactive mic collection + score against current detector",
    )
    parser.add_argument(
        "--score-session",
        type=Path,
        default=None,
        help="Re-score an existing session directory",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=2.5,
        help="Seconds to record per sample (default 2.5)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume the most recent session_* folder (keeps existing WAVs)",
    )
    parser.add_argument(
        "--resume-session",
        type=Path,
        default=None,
        help="Resume a specific session directory",
    )
    parser.add_argument(
        "--collect-hi-aira",
        action="store_true",
        help="Record 4 Hi Aira positives into existing Hey Aira session (does not touch Hey WAVs)",
    )
    parser.add_argument(
        "--session",
        type=Path,
        default=None,
        help="Calibration session directory (for --collect-hi-aira or --build)",
    )
    args = parser.parse_args()

    if args.collect_hi_aira:
        run_collect_hi_aira(
            seconds=max(1.5, min(4.0, float(args.seconds))),
            session=args.session,
        )
        return 0

    if args.score_session:
        if not args.score_session.is_dir():
            print("Session not found:", args.score_session)
            return 1
        score_session(args.score_session)
        return 0

    if args.calibrate or args.resume or args.resume_session:
        resume = args.resume_session
        if args.resume and resume is None:
            resume = _find_resume_session(None)
            if resume is None:
                print("No session to resume; starting fresh.")
        run_calibrate(
            seconds=max(1.5, min(4.0, float(args.seconds))),
            resume_session=resume if (args.resume or args.resume_session) else None,
        )
        return 0

    parser.print_help()
    print("\nExamples:")
    print("  python desktop/scripts/calibrate_wake_voice.py --calibrate")
    print("  python desktop/scripts/calibrate_wake_voice.py --collect-hi-aira")
    print(f"  Samples go to: {calibration_root()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
