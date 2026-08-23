"""
Full wake architecture evaluation — hard-negative contrast + scoring variants + verify rules.

Usage:
  python desktop/scripts/evaluate_wake_architecture.py

Uses production-equivalent preprocessing (prepare_production_wake_pcm) and the same
embedding path as live wake scoring.

Does NOT change production or rebuild AICA.exe.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop.launcher.voice_wake_diagnose import trace_wake_activation
from desktop.launcher.voice_wake_personal import (
    PersonalWakeProfile,
    load_generic_references,
    resolve_personal_wake,
    score_combined_margin,
)
from desktop.launcher.voice_wake_preprocess import prepare_production_wake_pcm
from desktop.launcher.voice_wake_util import OwwOnnxEmbedder
from desktop.launcher.voice_wake_verify import AMBIGUOUS_MARGIN_LOW, try_wake_whisper_only
from desktop.scripts.calibrate_wake_voice import calibration_root

HARD_NEG_SESSION = Path(
    r"C:\Users\Samrudh\AppData\Roaming\AICA\logs\wake_hard_negatives\hardneg_20260823_143133"
)

_ASSISTANT_BLOCK = frozenset({"siri", "google", "alexa", "cortana", "bixby"})
_WAKE_STARTERS = frozenset({"hey", "hay", "hi", "he"})
_NAME_TOKENS = frozenset({"ira", "aira", "aaira", "aida", "eira", "era", "eera", "ara", "eye ra", "i ra"})


def _read_pcm(wav: Path) -> bytes:
    with wave.open(str(wav), "rb") as wf:
        return wf.readframes(wf.getnframes())


def _normalize(vec: np.ndarray) -> np.ndarray:
    v = vec.astype(np.float32)
    v /= float(np.linalg.norm(v)) + 1e-9
    return v


def _embed_production(embedder: OwwOnnxEmbedder, pcm: bytes) -> tuple[np.ndarray, dict[str, Any]]:
    scored_pcm, prep = prepare_production_wake_pcm(pcm)
    if not prep.get("embed_ok"):
        raise ValueError(f"unembeddable pcm: {prep}")
    audio = np.frombuffer(scored_pcm, dtype=np.int16)
    vec = _normalize(embedder.embed_audio(audio))
    return vec, prep


def _load_all_samples() -> list[dict[str, Any]]:
    cal = Path(json.loads((calibration_root() / "latest_report.json").read_text())["session_dir"])
    rows: list[dict[str, Any]] = []
    for p in sorted((cal / "positive").glob("*.wav")):
        rows.append({"label": p.stem, "group": "positive_hey_aira", "path": p, "expect_a": True, "expect_b": True})
    for p in sorted((cal / "negative").glob("*.wav")):
        rows.append({"label": p.stem, "group": "negative_orig", "path": p, "expect_a": False, "expect_b": False})
    if HARD_NEG_SESSION.is_dir():
        for p in sorted(HARD_NEG_SESSION.glob("*.wav")):
            g = "hard_negative"
            expect_a = False
            expect_b = False
            if p.stem == "hard_hello_aira":
                g = "optional_hello_aira"
                expect_b = True
            rows.append({"label": p.stem, "group": g, "path": p, "expect_a": expect_a, "expect_b": expect_b})
    return rows


def _score_components(
    vec: np.ndarray,
    personal: PersonalWakeProfile,
    generic_neg: np.ndarray,
    hard_neg: np.ndarray | None,
) -> dict[str, float]:
    wake_emb = personal._wake_embeddings  # noqa: SLF001
    wake_c = personal._wake_centroid  # noqa: SLF001
    sample_sims = wake_emb @ vec if wake_emb is not None else np.array([])
    centroid_sim = float(vec @ _normalize(wake_c))
    max_sample_sim = float(np.max(sample_sims)) if sample_sims.size else centroid_sim
    generic_neg_sim = float(vec @ _normalize(generic_neg))
    hard_neg_sim = float(np.max(hard_neg @ vec)) if hard_neg is not None and hard_neg.size else 0.0
    max_neg = max(generic_neg_sim, hard_neg_sim)
    return {
        "centroid_sim": centroid_sim,
        "max_sample_sim": max_sample_sim,
        "generic_neg_sim": generic_neg_sim,
        "hard_neg_sim": hard_neg_sim,
        "max_neg_sim": max_neg,
    }


def _pos_sim(mode: str, comp: dict[str, float]) -> float:
    return {
        "centroid_only": comp["centroid_sim"],
        "max_sample_only": comp["max_sample_sim"],
        "current_hybrid": max(comp["centroid_sim"], comp["max_sample_sim"]),
        "blend_70_centroid": 0.7 * comp["centroid_sim"] + 0.3 * comp["max_sample_sim"],
        "blend_50": 0.5 * comp["centroid_sim"] + 0.5 * comp["max_sample_sim"],
    }[mode]


def _margin(mode: str, comp: dict[str, float], *, use_hard_neg: bool) -> float:
    neg = comp["max_neg_sim"] if use_hard_neg else comp["generic_neg_sim"]
    return _pos_sim(mode, comp) - neg


def verify_wake_current(text: str) -> bool:
    from desktop.launcher.voice_intents import detect_wake

    return detect_wake(text)


def verify_wake_structured_option_a(text: str) -> bool:
    """Option A: Hey/Hi + name token; block assistants; reject standalone name."""
    t = unicodedata.normalize("NFKC", text or "").lower().strip()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return False
    words = [w for w in t.split() if w]
    if not words:
        return False
    if any(w in _ASSISTANT_BLOCK for w in words):
        return False
    if len(words) == 1 and words[0] in _NAME_TOKENS:
        return False
    if words[0] not in _WAKE_STARTERS:
        return False
    rest = " ".join(words[1:])
    if any(tok in rest.split() for tok in _NAME_TOKENS):
        return True
    if any(tok.replace(" ", "") in rest.replace(" ", "") for tok in _NAME_TOKENS if " " in tok):
        return True
    if len(words) == 2 and words[0] in ("hey", "hi") and words[1] == "here":
        return True
    return False


def verify_wake_structured_option_b(text: str) -> bool:
    """Option B: Option A + Hello Aira."""
    t = unicodedata.normalize("NFKC", text or "").lower().strip()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return False
    words = [w for w in t.split() if w]
    if any(w in _ASSISTANT_BLOCK for w in words):
        return False
    if len(words) == 1 and words[0] in _NAME_TOKENS:
        return False
    starters = _WAKE_STARTERS | {"hello"}
    if words[0] not in starters:
        return False
    rest = " ".join(words[1:])
    if any(tok in rest.split() for tok in _NAME_TOKENS):
        return True
    if len(words) == 2 and words[0] in ("hey", "hi") and words[1] == "here":
        return True
    return False


def _whisper_fire(pcm: bytes, margin: float, verify_fn) -> tuple[bool, str]:
    if margin < AMBIGUOUS_MARGIN_LOW:
        return False, ""
    scored_pcm, _ = prepare_production_wake_pcm(pcm)
    fired, _, _, wx = try_wake_whisper_only(scored_pcm, margin=margin)
    if not wx:
        return False, ""
    if verify_fn(wx):
        return True, wx
    return False, wx


def evaluate_config(
    *,
    label: str,
    group: str,
    pcm: bytes,
    expect: bool,
    scoring_mode: str,
    threshold: float,
    embed_gate: float | None,
    use_whisper: bool,
    use_hard_neg: bool,
    verify_fn,
    personal: PersonalWakeProfile,
    generic_neg: np.ndarray,
    hard_neg: np.ndarray | None,
    embedder: OwwOnnxEmbedder,
    generic_wake: np.ndarray,
    use_production_combined: bool = False,
    require_whisper_verify: bool = False,
) -> dict[str, Any]:
    vec, prep = _embed_production(embedder, pcm)
    comp = _score_components(vec, personal, generic_neg, hard_neg if use_hard_neg else None)

    trace = trace_wake_activation(pcm, run_whisper=False)
    g_margin = float(trace["generic"]["margin"])

    if use_production_combined and not use_hard_neg:
        margin = float(trace["combined"]["margin"])
    else:
        margin = _margin(scoring_mode, comp, use_hard_neg=use_hard_neg)

    fired = False
    method = "none"
    whisper_text = ""
    reason = "reject"

    gate = embed_gate if embed_gate is not None else AMBIGUOUS_MARGIN_LOW

    if require_whisper_verify:
        if use_whisper and margin >= gate:
            wf, wx = _whisper_fire(pcm, margin, verify_fn)
            whisper_text = wx
            if wf:
                fired, method, reason = True, "whisper_verify", f"structured verify: {wx!r}"
            else:
                reason = f"embed {margin:.4f} candidate, whisper rejected: {wx!r}" if wx else "embed candidate, whisper empty"
        else:
            reason = f"margin {margin:.4f} below gate {gate}"
    elif margin >= threshold:
        fired, method, reason = True, "embedding_strong", f"{scoring_mode} margin {margin:.4f} >= {threshold}"
    elif use_whisper and margin >= gate:
        wf, wx = _whisper_fire(pcm, margin, verify_fn)
        whisper_text = wx
        if wf:
            fired, method, reason = True, "whisper_verify", f"whisper verify: {wx!r}"
        else:
            reason = f"whisper rejected: {wx!r}" if wx else "whisper no match"
    elif margin >= AMBIGUOUS_MARGIN_LOW:
        reason = f"ambiguous {margin:.4f} below gate {gate}"

    return {
        "label": label,
        "group": group,
        "expect_fire": expect,
        "scoring_mode": scoring_mode,
        "use_hard_neg": use_hard_neg,
        "centroid_sim": round(comp["centroid_sim"], 4),
        "max_sample_sim": round(comp["max_sample_sim"], 4),
        "generic_neg_sim": round(comp["generic_neg_sim"], 4),
        "hard_neg_sim": round(comp["hard_neg_sim"], 4),
        "max_neg_sim": round(comp["max_neg_sim"] if use_hard_neg else comp["generic_neg_sim"], 4),
        "margin": round(margin, 4),
        "generic_margin": round(g_margin, 4),
        "threshold": threshold,
        "embed_gate": embed_gate,
        "fired": fired,
        "method": method,
        "whisper_text": whisper_text,
        "reason": reason,
        "correct": fired == expect,
        "false_accept": fired and not expect,
        "false_reject": (not fired) and expect,
        "preprocess": prep.get("preprocess"),
    }


def _metrics(rows: list[dict], *, expect_key: str = "expect_fire") -> dict[str, Any]:
    pos = [r for r in rows if r.get(expect_key)]
    neg = [r for r in rows if not r.get(expect_key)]
    tp = sum(1 for r in pos if r["fired"])
    fn = sum(1 for r in pos if not r["fired"])
    tn = sum(1 for r in neg if not r["fired"])
    fp = sum(1 for r in neg if r["fired"])
    prec = round(tp / (tp + fp), 3) if (tp + fp) else None
    rec = round(tp / (tp + fn), 3) if (tp + fn) else None
    return {
        "true_positives": tp,
        "false_negatives": fn,
        "true_negatives": tn,
        "false_positives": fp,
        "positives_total": len(pos),
        "negatives_total": len(neg),
        "precision": prec,
        "recall": rec,
    }


def main() -> int:
    active, thr, personal = resolve_personal_wake()
    if not personal:
        raise SystemExit("Personal wake profile not loaded")
    threshold = float(thr or 0.05)
    generic_wake, generic_neg = load_generic_references()
    embedder = OwwOnnxEmbedder()

    hard_paths = sorted(HARD_NEG_SESSION.glob("*.wav"))
    hard_neg = (
        np.stack([_embed_production(embedder, _read_pcm(p))[0] for p in hard_paths])
        if hard_paths
        else None
    )

    samples = _load_all_samples()

    configs: list[dict[str, Any]] = [
        {
            "name": "current_production",
            "scoring": "current_hybrid",
            "hard_neg": False,
            "whisper": True,
            "verify": "current",
            "gate": None,
            "production_combined": True,
        },
        {
            "name": "current_production_no_whisper",
            "scoring": "current_hybrid",
            "hard_neg": False,
            "whisper": False,
            "verify": "current",
            "gate": None,
            "production_combined": True,
        },
        {
            "name": "hard_neg_contrast_hybrid",
            "scoring": "current_hybrid",
            "hard_neg": True,
            "whisper": False,
            "verify": "structured_a",
            "gate": None,
            "production_combined": False,
        },
        {
            "name": "hard_neg_contrast_centroid",
            "scoring": "centroid_only",
            "hard_neg": True,
            "whisper": False,
            "verify": "structured_a",
            "gate": None,
            "production_combined": False,
        },
        {
            "name": "hard_neg_contrast_max_sample",
            "scoring": "max_sample_only",
            "hard_neg": True,
            "whisper": False,
            "verify": "structured_a",
            "gate": None,
            "production_combined": False,
        },
        {
            "name": "hard_neg_blend_70_centroid",
            "scoring": "blend_70_centroid",
            "hard_neg": True,
            "whisper": False,
            "verify": "structured_a",
            "gate": None,
            "production_combined": False,
        },
        {
            "name": "hard_neg_blend_50",
            "scoring": "blend_50",
            "hard_neg": True,
            "whisper": False,
            "verify": "structured_a",
            "gate": None,
            "production_combined": False,
        },
        {
            "name": "two_stage_hybrid_hardneg_whisper_a",
            "scoring": "current_hybrid",
            "hard_neg": True,
            "whisper": True,
            "verify": "structured_a",
            "gate": 0.0,
            "production_combined": False,
            "strong_requires_verify": True,
        },
        {
            "name": "two_stage_hybrid_hardneg_whisper_a_gate002",
            "scoring": "current_hybrid",
            "hard_neg": True,
            "whisper": True,
            "verify": "structured_a",
            "gate": 0.02,
            "production_combined": False,
            "strong_requires_verify": True,
        },
        {
            "name": "two_stage_blend70_hardneg_whisper_a",
            "scoring": "blend_70_centroid",
            "hard_neg": True,
            "whisper": True,
            "verify": "structured_a",
            "gate": 0.0,
            "production_combined": False,
            "strong_requires_verify": True,
        },
        {
            "name": "whisper_only_structured_a",
            "scoring": "current_hybrid",
            "hard_neg": False,
            "whisper": True,
            "verify": "structured_a",
            "gate": AMBIGUOUS_MARGIN_LOW,
            "production_combined": True,
            "require_whisper_verify": True,
        },
    ]

    verify_fns = {
        "current": verify_wake_current,
        "structured_a": verify_wake_structured_option_a,
        "structured_b": verify_wake_structured_option_b,
    }

    all_rows: list[dict] = []
    comparison: dict[str, Any] = {}

    for cfg in configs:
        verify_fn = verify_fns[cfg["verify"]]
        cfg_rows = []
        for s in samples:
            pcm = _read_pcm(s["path"])
            row = evaluate_config(
                label=s["label"],
                group=s["group"],
                pcm=pcm,
                expect=s["expect_a"],
                scoring_mode=cfg["scoring"],
                threshold=threshold,
                embed_gate=cfg["gate"],
                use_whisper=cfg["whisper"],
                use_hard_neg=cfg["hard_neg"],
                verify_fn=verify_fn,
                personal=personal,
                generic_neg=generic_neg,
                hard_neg=hard_neg,
                embedder=embedder,
                generic_wake=generic_wake,
                use_production_combined=cfg.get("production_combined", False),
                require_whisper_verify=cfg.get("require_whisper_verify", False) or cfg.get("strong_requires_verify", False),
            )
            row["config"] = cfg["name"]
            cfg_rows.append(row)
            all_rows.append(row)

        hard_fp = [r["label"] for r in cfg_rows if r["fired"] and r["group"] == "hard_negative"]
        comparison[cfg["name"]] = {
            "option_a": _metrics(cfg_rows, expect_key="expect_fire"),
            "hard_neg_fires": hard_fp,
            "missed_positives": [r["label"] for r in cfg_rows if not r["fired"] and r["group"] == "positive_hey_aira"],
            "per_sample": [{k: r[k] for k in ("label", "group", "margin", "fired", "method", "reason")} for r in cfg_rows],
        }

    # Option B metrics
    for cfg in configs:
        name = cfg["name"]
        verify_fn = verify_wake_structured_option_b
        cfg_rows = []
        for s in samples:
            pcm = _read_pcm(s["path"])
            row = evaluate_config(
                label=s["label"],
                group=s["group"],
                pcm=pcm,
                expect=s["expect_b"],
                scoring_mode=cfg["scoring"],
                threshold=threshold,
                embed_gate=cfg["gate"],
                use_whisper=cfg["whisper"],
                use_hard_neg=cfg["hard_neg"],
                verify_fn=verify_fn,
                personal=personal,
                generic_neg=generic_neg,
                hard_neg=hard_neg,
                embedder=embedder,
                generic_wake=generic_wake,
                use_production_combined=cfg.get("production_combined", False),
                require_whisper_verify=cfg.get("require_whisper_verify", False) or cfg.get("strong_requires_verify", False),
            )
            cfg_rows.append(row)
        comparison[name]["option_b"] = _metrics(cfg_rows, expect_key="expect_fire")

    # Threshold sweep for recommended two-stage candidate
    sweep_rows: list[dict] = []
    for thr_s in [0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10, 0.12, 0.15]:
        cfg_rows = []
        for s in samples:
            pcm = _read_pcm(s["path"])
            row = evaluate_config(
                label=s["label"],
                group=s["group"],
                pcm=pcm,
                expect=s["expect_a"],
                scoring_mode="blend_70_centroid",
                threshold=thr_s,
                embed_gate=0.0,
                use_whisper=True,
                use_hard_neg=True,
                verify_fn=verify_wake_structured_option_a,
                personal=personal,
                generic_neg=generic_neg,
                hard_neg=hard_neg,
                embedder=embedder,
                generic_wake=generic_wake,
                require_whisper_verify=True,
            )
            cfg_rows.append(row)
        m = _metrics(cfg_rows)
        m["threshold"] = thr_s
        m["hard_neg_fp"] = [r["label"] for r in cfg_rows if r["fired"] and r["group"] == "hard_negative"]
        sweep_rows.append(m)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "threshold": threshold,
        "hard_neg_session": str(HARD_NEG_SESSION),
        "hard_negative_count": len(hard_paths),
        "positive_count": sum(1 for s in samples if s["group"] == "positive_hey_aira"),
        "product_option_a": "Hey Aira + Hi Aira (calibration positives); reject standalone Aira/Ira",
        "product_option_b": "Option A + Hello Aira",
        "comparison": comparison,
        "threshold_sweep_blend70_two_stage": sweep_rows,
        "current_production_hard_neg_margins": [
            {k: r[k] for k in ("label", "centroid_sim", "max_sample_sim", "margin", "fired", "reason")}
            for r in all_rows
            if r["config"] == "current_production_no_whisper" and r["group"] == "hard_negative"
        ],
    }

    out = calibration_root() / "wake_architecture_evaluation.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n======== WAKE ARCHITECTURE EVALUATION (production preprocessing) ========")
    print(f"Threshold: {threshold}  Hard negs: {len(hard_paths)}  Positives: 12")
    print("\nConfig                          | Recall | FP | Precision | hard FP")
    for name, c in comparison.items():
        m = c["option_a"]
        hn = c["hard_neg_fires"]
        print(
            f"{name:32}| {m['true_positives']:2}/{m['positives_total']}   "
            f"| {m['false_positives']:2} | {m['precision']}     | {hn[:4]}"
        )
    print("\nThreshold sweep (blend70 + hardneg + structured whisper A):")
    for s in sweep_rows:
        print(
            f"  thr={s['threshold']:.2f} recall={s['recall']} fp={s['false_positives']} "
            f"hard_fp={s['hard_neg_fp']}"
        )
    print("\nWrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
