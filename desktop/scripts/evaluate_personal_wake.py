"""
Offline evaluation of generic vs personalized Hey IRA wake scoring.

Usage:
  python desktop/scripts/build_personal_wake.py
  python desktop/scripts/evaluate_personal_wake.py

Does NOT change production thresholds or enable personal wake in Desktop.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop.launcher.voice_paths import personal_wake_meta_path, personal_wake_npz_path
from desktop.launcher.voice_wake_personal import (
    PersonalWakeProfile,
    load_generic_references,
    score_combined_margin,
    score_generic_margin,
)
from desktop.launcher.voice_wake_util import OwwOnnxEmbedder
from desktop.launcher.voice_wake_verify import AMBIGUOUS_MARGIN_LOW, STRONG_MARGIN_DEFAULT
from desktop.scripts.calibrate_wake_voice import calibration_root


def default_session() -> Path:
    latest = calibration_root() / "latest_report.json"
    if latest.is_file():
        data = json.loads(latest.read_text(encoding="utf-8"))
        return Path(data["session_dir"])
    sessions = sorted(calibration_root().glob("session_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not sessions:
        raise SystemExit("No calibration session found.")
    return sessions[0]


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


def _load_session_samples(session: Path) -> tuple[list[tuple[str, Path]], list[tuple[str, Path]]]:
    pos = [(p.stem, p) for p in sorted((session / "positive").glob("*.wav"))]
    neg = [(p.stem, p) for p in sorted((session / "negative").glob("*.wav"))]
    return pos, neg


def _read_pcm(wav_path: Path) -> bytes:
    with wave.open(str(wav_path), "rb") as wf:
        return wf.readframes(wf.getnframes())


def _evaluate_mode(
    samples: list[tuple[str, str, Path]],
    *,
    mode: str,
    generic_wake: np.ndarray,
    generic_neg: np.ndarray,
    personal: PersonalWakeProfile | None,
    embedder: OwwOnnxEmbedder,
    use_padding: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for kind, label, wav_path in samples:
        pcm = _read_pcm(wav_path)
        row: dict[str, Any] = {"label": label, "kind": kind, "file": str(wav_path), "mode": mode}

        if mode == "generic_before":
            _, _, margin, meta = score_generic_margin(
                pcm,
                wake_centroid=generic_wake,
                neg_centroid=generic_neg,
                embedder=embedder,
                use_padding=False,
            )
            row.update(
                {
                    "margin": None if not meta.get("embed_ok", True) else round(margin, 4),
                    "wake_sim": meta.get("wake_sim"),
                    "neg_sim": meta.get("neg_sim"),
                    "preprocess": meta.get("preprocess"),
                    "scorable": meta.get("embed_ok", margin > -0.99),
                    "error": meta.get("error"),
                }
            )
        elif mode == "generic_padded":
            _, _, margin, meta = score_generic_margin(
                pcm,
                wake_centroid=generic_wake,
                neg_centroid=generic_neg,
                embedder=embedder,
                use_padding=True,
            )
            row.update(
                {
                    "margin": round(margin, 4),
                    "wake_sim": meta.get("wake_sim"),
                    "neg_sim": meta.get("neg_sim"),
                    "preprocess": meta.get("preprocess"),
                    "scorable": meta.get("embed_ok", True),
                }
            )
        elif mode == "combined":
            assert personal is not None
            scored = score_combined_margin(
                pcm,
                generic_wake=generic_wake,
                generic_neg=generic_neg,
                personal=personal,
                embedder=embedder,
            )
            row.update(
                {
                    "margin_generic": round(scored["generic"]["margin"], 4),
                    "margin_personal": round(scored["personal"]["margin"], 4),
                    "margin": round(scored["combined"]["margin"], 4),
                    "wake_sim": round(scored["combined"]["wake_sim"], 4),
                    "neg_sim": round(scored["combined"]["neg_sim"], 4),
                    "preprocess": scored["generic"]["meta"].get("preprocess"),
                    "scorable": True,
                }
            )
        else:
            raise ValueError(mode)
        rows.append(row)
    return rows


def _threshold_sweep(
    pos_margins: list[float],
    neg_margins: list[float],
    *,
    thresholds: list[float] | None = None,
) -> list[dict[str, Any]]:
    if thresholds is None:
        thresholds = [round(x, 4) for x in np.arange(-0.08, 0.06, 0.005)]
    rows = []
    for thr in thresholds:
        tp = sum(1 for m in pos_margins if m >= thr)
        fp = sum(1 for m in neg_margins if m >= thr)
        fn = len(pos_margins) - tp
        tn = len(neg_margins) - fp
        rows.append(
            {
                "threshold": thr,
                "true_positives": tp,
                "false_positives": fp,
                "false_negatives": fn,
                "true_negatives": tn,
                "pos_recall": round(tp / len(pos_margins), 3) if pos_margins else 0,
                "neg_rejection": round(tn / len(neg_margins), 3) if neg_margins else 0,
            }
        )
    return rows


def _recommend_threshold(sweep: list[dict[str, Any]], *, pos_n: int, neg_n: int) -> dict[str, Any]:
    """Pick threshold with zero FP and max TP; else lowest FP with best recall."""
    zero_fp = [r for r in sweep if r["false_positives"] == 0]
    if zero_fp:
        best = max(zero_fp, key=lambda r: (r["true_positives"], -r["threshold"]))
        return {
            "threshold": best["threshold"],
            "true_positives": best["true_positives"],
            "false_positives": 0,
            "false_negatives": best["false_negatives"],
            "pos_recall": best["pos_recall"],
            "neg_rejection": best["neg_rejection"],
            "criterion": "zero false positives, maximize true positives",
        }

    best = min(sweep, key=lambda r: (r["false_positives"], -r["true_positives"]))
    return {
        "threshold": best["threshold"],
        "true_positives": best["true_positives"],
        "false_positives": best["false_positives"],
        "false_negatives": best["false_negatives"],
        "pos_recall": best["pos_recall"],
        "neg_rejection": best["neg_rejection"],
        "criterion": "no zero-FP threshold; minimized false positives",
    }


def _summarize(rows: list[dict[str, Any]], *, current_threshold: float = STRONG_MARGIN_DEFAULT) -> dict[str, Any]:
    pos = [r for r in rows if r["kind"] == "positive" and r.get("scorable") and r.get("margin") is not None]
    neg = [r for r in rows if r["kind"] == "negative" and r.get("scorable") and r.get("margin") is not None]
    pos_m = [float(r["margin"]) for r in pos]
    neg_m = [float(r["margin"]) for r in neg]
    pos_stats = _stats(pos_m)
    neg_stats = _stats(neg_m)
    overlap = None
    if pos_m and neg_m:
        overlap = {
            "pos_min": pos_stats["min"],
            "neg_max": neg_stats["max"],
            "gap": round(pos_stats["min"] - neg_stats["max"], 4),
            "distributions_overlap": pos_stats["min"] < neg_stats["max"],
        }
    sweep = _threshold_sweep(pos_m, neg_m)
    recommended = _recommend_threshold(sweep, pos_n=len(pos_m), neg_n=len(neg_m))
    return {
        "positive_stats": pos_stats,
        "negative_stats": neg_stats,
        "overlap": overlap,
        "current_threshold": current_threshold,
        "at_current_threshold": {
            "positives_fired": sum(1 for m in pos_m if m >= current_threshold),
            "negatives_fired": sum(1 for m in neg_m if m >= current_threshold),
        },
        "threshold_sweep": sweep,
        "recommended_threshold": recommended,
        "unscorable": [r["label"] for r in rows if not r.get("scorable")],
    }


def evaluate_session(session: Path) -> dict[str, Any]:
    if not personal_wake_npz_path().is_file():
        raise SystemExit(
            "Personal profile missing. Run: python desktop/scripts/build_personal_wake.py"
        )

    pos_paths, neg_paths = _load_session_samples(session)
    samples = [("positive", label, p) for label, p in pos_paths] + [
        ("negative", label, p) for label, p in neg_paths
    ]

    generic_wake, generic_neg = load_generic_references()
    embedder = OwwOnnxEmbedder()
    personal = PersonalWakeProfile()
    personal.ensure_loaded()

    generic_before = _evaluate_mode(
        samples,
        mode="generic_before",
        generic_wake=generic_wake,
        generic_neg=generic_neg,
        personal=None,
        embedder=embedder,
        use_padding=False,
    )
    generic_padded = _evaluate_mode(
        samples,
        mode="generic_padded",
        generic_wake=generic_wake,
        generic_neg=generic_neg,
        personal=None,
        embedder=embedder,
        use_padding=True,
    )
    combined = _evaluate_mode(
        samples,
        mode="combined",
        generic_wake=generic_wake,
        generic_neg=generic_neg,
        personal=personal,
        embedder=embedder,
        use_padding=True,
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_dir": str(session),
        "personal_profile": str(personal_wake_npz_path()),
        "personal_meta": str(personal_wake_meta_path()),
        "production_personal_wake_enabled": os.environ.get("AICA_PERSONAL_WAKE", ""),
        "current_strong_threshold": STRONG_MARGIN_DEFAULT,
        "ambiguous_low": AMBIGUOUS_MARGIN_LOW,
        "comparison": {
            "generic_before": {
                "description": "Legacy production: VAD clip only, no padding, generic references",
                "samples": generic_before,
                "summary": _summarize(generic_before),
            },
            "generic_padded": {
                "description": "Generic references + production-equivalent padding fix",
                "samples": generic_padded,
                "summary": _summarize(generic_padded),
            },
            "combined_personal": {
                "description": "Generic + personal references, max margin, with padding",
                "samples": combined,
                "summary": _summarize(combined),
            },
        },
    }
    return report


def print_report(report: dict[str, Any]) -> None:
    print("\n======== PERSONAL WAKE EVALUATION ========")
    print("Session:", report["session_dir"])
    print("Personal profile:", report["personal_profile"])
    print("Production AICA_PERSONAL_WAKE:", repr(report["production_personal_wake_enabled"] or "(not set)"))
    print("Current strong threshold:", report["current_strong_threshold"])

    for key, block in report["comparison"].items():
        s = block["summary"]
        print(f"\n--- {key} ---")
        print(block["description"])
        print("POS:", json.dumps(s["positive_stats"], indent=2))
        print("NEG:", json.dumps(s["negative_stats"], indent=2))
        print("OVERLAP:", json.dumps(s["overlap"], indent=2))
        print("At current threshold 0.02:", json.dumps(s["at_current_threshold"]))
        rec = s["recommended_threshold"]
        print(
            f"RECOMMENDED threshold: {rec['threshold']} "
            f"(TP={rec['true_positives']} FP={rec['false_positives']} "
            f"FN={rec['false_negatives']} recall={rec['pos_recall']})"
        )
        if s["unscorable"]:
            print("UNSCORABLE:", s["unscorable"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate personalized Hey IRA wake profile")
    parser.add_argument("--session", type=Path, default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    session = (args.session or default_session()).resolve()
    report = evaluate_session(session)

    rec_thr = (
        report["comparison"]["combined_personal"]["summary"]["recommended_threshold"]["threshold"]
    )
    from desktop.launcher.voice_wake_personal import update_recommended_threshold

    update_recommended_threshold(rec_thr)
    report["recommended_operating_threshold"] = rec_thr

    out = args.json_out or (calibration_root() / "personal_wake_evaluation.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print_report(report)
    print("\nWrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
