"""
Phase 2 full wake validation — hard-neg contrast + structured Whisper verify.

Usage:
  python desktop/scripts/evaluate_wake_phase2_validation.py

Requires:
  - personal_wake_hard_neg_embeddings.npz (build_hard_neg_wake.py)
  - personal_hey_ira profile (build_personal_wake.py after Hi Aira collection)

Does NOT rebuild or deploy AICA.exe.
"""
from __future__ import annotations

import json
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop.launcher.voice_paths import (
    hard_neg_wake_meta_path,
    hard_neg_wake_npz_path,
    personal_wake_meta_path,
    personal_wake_npz_path,
)
from desktop.launcher.voice_wake_diagnose import trace_via_production_detector
from desktop.launcher.voice_wake_personal import (
    PERSONAL_WAKE_STRONG_THRESHOLD_V2,
    resolve_hard_neg_wake,
    resolve_personal_wake,
)
from desktop.launcher.voice_wake_verify import AMBIGUOUS_MARGIN_LOW
from desktop.scripts.calibrate_wake_voice import calibration_root

HARD_NEG_SESSION = Path(
    r"C:\Users\Samrudh\AppData\Roaming\AICA\logs\wake_hard_negatives\hardneg_20260823_143133"
)

# Per user spec — expected outcomes for key samples
KEY_EXPECTATIONS: dict[str, bool] = {
    "hard_mamacita": False,
    "hard_hey_siri": False,
    "hard_hey_google": False,
    "hard_aira_alone": False,
    "hard_ira_alone": False,
    "hard_hello_aira": False,
    "hard_anita": False,
    "hard_weather": False,
}


def _read_pcm(wav: Path) -> bytes:
    with wave.open(str(wav), "rb") as wf:
        return wf.readframes(wf.getnframes())


def _load_samples() -> list[dict[str, Any]]:
    cal = Path(json.loads((calibration_root() / "latest_report.json").read_text())["session_dir"])
    rows: list[dict[str, Any]] = []
    for p in sorted((cal / "positive").glob("*.wav")):
        group = "positive_hi_aira" if p.stem.startswith("pos_hi_") else "positive_hey_aira"
        rows.append({"label": p.stem, "group": group, "path": p, "expect_fire": True})
    for p in sorted((cal / "negative").glob("*.wav")):
        rows.append({"label": p.stem, "group": "negative_orig", "path": p, "expect_fire": False})
    if HARD_NEG_SESSION.is_dir():
        for p in sorted(HARD_NEG_SESSION.glob("*.wav")):
            rows.append(
                {
                    "label": p.stem,
                    "group": "hard_negative",
                    "path": p,
                    "expect_fire": KEY_EXPECTATIONS.get(p.stem, False),
                }
            )
    return rows


def _evaluate_sample(sample: dict[str, Any], threshold: float) -> dict[str, Any]:
    pcm = _read_pcm(sample["path"])
    trace = trace_via_production_detector(pcm)
    prod = trace.get("production_try_wake_on_pcm", {})
    fired = bool(prod.get("fired"))
    generic = trace.get("generic") or {}
    personal = trace.get("personal") or {}
    combined = trace.get("combined") or {}
    decision = trace.get("decision") or {}
    whisper_invoked = decision.get("activation_method") == "ambiguous_whisper_candidate" or bool(
        decision.get("whisper_transcript")
    ) or prod.get("method") == "whisper_verify"

    return {
        "label": sample["label"],
        "group": sample["group"],
        "expect_fire": sample["expect_fire"],
        "fired": fired,
        "correct": fired == sample["expect_fire"],
        "generic_margin": generic.get("margin"),
        "personal_wake_sim": personal.get("wake_sim"),
        "personal_margin": personal.get("margin"),
        "centroid_sim": personal.get("centroid_sim"),
        "max_sample_sim": personal.get("max_sample_sim"),
        "generic_neg_sim": personal.get("generic_neg_sim"),
        "hard_neg_sim": personal.get("hard_neg_sim"),
        "combined_margin": combined.get("margin"),
        "threshold": threshold,
        "ambiguous_low": AMBIGUOUS_MARGIN_LOW,
        "whisper_invoked": whisper_invoked,
        "whisper_transcript": decision.get("whisper_transcript") or prod.get("whisper_text") or "",
        "activation_method": prod.get("method") or decision.get("activation_method"),
        "reason": decision.get("reason"),
    }


def main() -> int:
    active, thr, personal = resolve_personal_wake()
    hn_active, hard_neg = resolve_hard_neg_wake()
    threshold = PERSONAL_WAKE_STRONG_THRESHOLD_V2
    if thr is not None and float(thr) != threshold:
        print(f"NOTE: personal meta threshold is {thr}; Phase 2 validation uses {threshold}")

    print("\n======== PHASE 2 WAKE VALIDATION ========")
    print("Personal active:", active, "| Hard neg loaded:", hn_active)
    print("Threshold:", threshold, "| Ambiguous low:", AMBIGUOUS_MARGIN_LOW)
    print("Personal NPZ:", personal_wake_npz_path(), "exists:", personal_wake_npz_path().is_file())
    print("Hard neg NPZ:", hard_neg_wake_npz_path(), "exists:", hard_neg_wake_npz_path().is_file())

    if not hn_active:
        print("\nERROR: Hard-negative profile missing. Run:")
        print("  python desktop/scripts/build_hard_neg_wake.py")
        return 1

    samples = _load_samples()
    hey = [s for s in samples if s["group"] == "positive_hey_aira"]
    hi = [s for s in samples if s["group"] == "positive_hi_aira"]
    if not hi:
        print("\nNOTE: No Hi Aira samples yet. Record with:")
        print("  python desktop/scripts/calibrate_wake_voice.py --collect-hi-aira")
        print("Then rebuild profile:")
        print("  python desktop/scripts/build_personal_wake.py")

    rows = [_evaluate_sample(s, threshold) for s in samples]

    pos = [r for r in rows if r["expect_fire"]]
    neg = [r for r in rows if not r["expect_fire"]]
    tp = sum(1 for r in pos if r["fired"])
    fn = sum(1 for r in pos if not r["fired"])
    fp = sum(1 for r in neg if r["fired"])
    tn = sum(1 for r in neg if not r["fired"])
    prec = round(tp / (tp + fp), 3) if (tp + fp) else None
    rec = round(tp / (tp + fn), 3) if (tp + fn) else None

    hey_rows = [r for r in rows if r["group"] == "positive_hey_aira"]
    hi_rows = [r for r in rows if r["group"] == "positive_hi_aira"]
    hard_rows = [r for r in rows if r["group"] == "hard_negative"]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": 2,
        "architecture": "hard_neg_contrast + personal_primary + structured_whisper",
        "threshold": threshold,
        "ambiguous_low": AMBIGUOUS_MARGIN_LOW,
        "personal_meta": str(personal_wake_meta_path()) if personal_wake_meta_path().is_file() else None,
        "hard_neg_meta": str(hard_neg_wake_meta_path()) if hard_neg_wake_meta_path().is_file() else None,
        "hey_aira_count": len(hey_rows),
        "hi_aira_count": len(hi_rows),
        "metrics": {
            "true_positives": tp,
            "false_negatives": fn,
            "true_negatives": tn,
            "false_positives": fp,
            "precision": prec,
            "recall": rec,
            "hey_aira_recall": f"{sum(1 for r in hey_rows if r['fired'])}/{len(hey_rows)}",
            "hi_aira_recall": f"{sum(1 for r in hi_rows if r['fired'])}/{len(hi_rows)}" if hi_rows else "N/A",
        },
        "key_hard_negative_results": {
            r["label"]: {
                "fired": r["fired"],
                "margin": r["combined_margin"],
                "hard_neg_sim": r["hard_neg_sim"],
                "correct": r["correct"],
            }
            for r in hard_rows
            if r["label"] in KEY_EXPECTATIONS
        },
        "samples": rows,
    }

    out = calibration_root() / "wake_phase2_validation.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\nRecall: {tp}/{len(pos)}  FP: {fp}  FN: {fn}  Precision: {prec}")
    print(f"Hey Aira: {report['metrics']['hey_aira_recall']}  Hi Aira: {report['metrics']['hi_aira_recall']}")
    print("\nKey hard negatives:")
    for label, info in report["key_hard_negative_results"].items():
        status = "PASS" if info["correct"] else "FAIL"
        print(f"  [{status}] {label:22} fired={info['fired']} margin={info['margin']}")

    if fn:
        print("\nFalse negatives:")
        for r in rows:
            if r["expect_fire"] and not r["fired"]:
                print(f"  {r['label']} margin={r['combined_margin']} reason={r['reason']}")
    if fp:
        print("\nFalse positives:")
        for r in rows:
            if not r["expect_fire"] and r["fired"]:
                print(f"  {r['label']} margin={r['combined_margin']} method={r['activation_method']}")

    print("\nWrote", out)
    return 0 if fp == 0 and fn == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
