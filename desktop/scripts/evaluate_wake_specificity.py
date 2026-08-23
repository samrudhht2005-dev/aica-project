"""
Evaluate wake specificity fixes OFFLINE — does not change production.

Compares CURRENT scoring vs PROPOSED rules on calibration + hard-negative samples.

Usage:
  python desktop/scripts/evaluate_wake_specificity.py
  python desktop/scripts/evaluate_wake_specificity.py --hard-negs

Proposed rules evaluated:
  1. current — max(generic, personal) vs threshold 0.05
  2. whisper_gate — personal strong-fire only if generic_margin >= 0, else Whisper verify
  3. hard_neg_contrast — personal margin vs max(generic_neg, hard_neg embeddings)
  4. centroid_only — personal uses centroid only (not max sample match)

Does NOT rebuild AICA.exe.
"""
from __future__ import annotations

import argparse
import json
import sys
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
)
from desktop.launcher.voice_wake_preprocess import ensure_embeddable_pcm
from desktop.launcher.voice_wake_util import OwwOnnxEmbedder, ensure_oww_backbone
from desktop.launcher.voice_wake_verify import AMBIGUOUS_MARGIN_LOW, try_wake_whisper_only
from desktop.scripts.calibrate_wake_voice import calibration_root


def _read_pcm(wav: Path) -> bytes:
    with wave.open(str(wav), "rb") as wf:
        return wf.readframes(wf.getnframes())


def _load_samples() -> list[tuple[str, str, Path]]:
    latest = calibration_root() / "latest_report.json"
    session = Path(json.loads(latest.read_text())["session_dir"])
    rows = [(p.stem, "positive", p) for p in sorted((session / "positive").glob("*.wav"))]
    rows += [(p.stem, "negative", p) for p in sorted((session / "negative").glob("*.wav"))]
    return rows


def _load_hard_negs() -> list[tuple[str, str, Path]]:
    from desktop.scripts.diagnose_false_wake import hard_negatives_root

    latest = hard_negatives_root() / "latest_report.json"
    if not latest.is_file():
        return []
    session = Path(json.loads(latest.read_text())["session_dir"])
    return [(p.stem, "hard_negative", p) for p in sorted(session.glob("*.wav"))]


def _embed_pcm(embedder: OwwOnnxEmbedder, pcm: bytes) -> np.ndarray:
    scored, _ = ensure_embeddable_pcm(pcm)
    audio = np.frombuffer(scored, dtype=np.int16)
    vec = embedder.embed_audio(audio).astype(np.float32)
    vec /= float(np.linalg.norm(vec)) + 1e-9
    return vec


def _build_hard_neg_matrix(paths: list[Path], embedder: OwwOnnxEmbedder) -> np.ndarray | None:
    if not paths:
        return None
    vecs = [_embed_pcm(embedder, _read_pcm(p)) for p in paths]
    return np.stack(vecs, axis=0)


def evaluate_rule(
    *,
    label: str,
    kind: str,
    pcm: bytes,
    threshold: float,
    generic_wake: np.ndarray,
    generic_neg: np.ndarray,
    personal: PersonalWakeProfile | None,
    embedder: OwwOnnxEmbedder,
    hard_neg_matrix: np.ndarray | None,
    rule: str,
) -> dict[str, Any]:
    trace = trace_wake_activation(pcm, run_whisper=False)
    g = trace["generic"]
    p = trace.get("personal")
    g_margin = float(g["margin"])
    p_margin = float(p["margin"]) if p else g_margin
    combined = float(trace["combined"]["margin"])

    fired = False
    method = "none"
    reason = "reject"
    effective_margin = combined

    if rule == "current":
        effective_margin = combined
        if combined >= threshold:
            fired, method, reason = True, "embedding_strong", "current combined >= threshold"
        elif combined >= AMBIGUOUS_MARGIN_LOW:
            scored_pcm, _ = ensure_embeddable_pcm(pcm)
            wf, _, m, wx = try_wake_whisper_only(scored_pcm, margin=combined)
            if wf:
                fired, method, reason = True, "whisper_verify", f"whisper: {wx!r}"

    elif rule == "whisper_gate":
        # Personal-only strong fires must pass generic gate or go to Whisper.
        if p and p_margin > g_margin and g_margin < 0.0 and p_margin >= threshold:
            scored_pcm, _ = ensure_embeddable_pcm(pcm)
            wf, _, m, wx = try_wake_whisper_only(scored_pcm, margin=p_margin)
            effective_margin = p_margin
            if wf:
                fired, method, reason = True, "whisper_verify", f"personal-only gated: {wx!r}"
            else:
                reason = f"personal {p_margin:.4f} but generic {g_margin:.4f}<0, whisper rejected"
        elif combined >= threshold:
            fired, method, reason = True, "embedding_strong", "combined >= threshold"
            effective_margin = combined
        elif combined >= AMBIGUOUS_MARGIN_LOW:
            scored_pcm, _ = ensure_embeddable_pcm(pcm)
            wf, _, m, wx = try_wake_whisper_only(scored_pcm, margin=combined)
            if wf:
                fired, method, reason = True, "whisper_verify", f"whisper: {wx!r}"

    elif rule == "hard_neg_contrast":
        if p and personal and hard_neg_matrix is not None:
            scored_pcm, _ = ensure_embeddable_pcm(pcm)
            vec = _embed_pcm(embedder, scored_pcm)
            wake_c = personal._wake_centroid  # noqa: SLF001
            sample_sims = personal._wake_embeddings @ vec if personal._wake_embeddings is not None else []  # noqa: SLF001
            wake_sim = max(float(vec @ wake_c), float(np.max(sample_sims)) if len(sample_sims) else 0.0)
            neg_sim = max(float(vec @ generic_neg), float(np.max(hard_neg_matrix @ vec)))
            effective_margin = wake_sim - neg_sim
            if effective_margin >= threshold:
                fired, method, reason = True, "embedding_strong", "hard-neg contrast >= threshold"
            elif effective_margin >= AMBIGUOUS_MARGIN_LOW:
                wf, _, m, wx = try_wake_whisper_only(scored_pcm, margin=effective_margin)
                if wf:
                    fired, method, reason = True, "whisper_verify", f"whisper: {wx!r}"
        else:
            effective_margin = combined
            if combined >= threshold:
                fired, method, reason = True, "embedding_strong", "fallback current"

    elif rule == "centroid_only":
        if p and personal:
            scored_pcm, _ = ensure_embeddable_pcm(pcm)
            vec = _embed_pcm(embedder, scored_pcm)
            wake_sim = float(vec @ personal._wake_centroid)  # noqa: SLF001
            neg_sim = float(vec @ generic_neg)
            effective_margin = wake_sim - neg_sim
            if effective_margin >= threshold:
                fired, method, reason = True, "embedding_strong", "centroid-only >= threshold"
            elif effective_margin >= AMBIGUOUS_MARGIN_LOW:
                wf, _, m, wx = try_wake_whisper_only(scored_pcm, margin=effective_margin)
                if wf:
                    fired, method, reason = True, "whisper_verify", f"whisper: {wx!r}"
        else:
            effective_margin = combined
            if combined >= threshold:
                fired, method, reason = True, "embedding_strong", "fallback current"

    expect_fire = kind == "positive"
    return {
        "label": label,
        "kind": kind,
        "rule": rule,
        "generic_margin": round(g_margin, 4),
        "personal_margin": round(p_margin, 4) if p else None,
        "effective_margin": round(effective_margin, 4),
        "threshold": threshold,
        "fired": fired,
        "method": method,
        "reason": reason,
        "expect_fire": expect_fire,
        "correct": fired == expect_fire if kind in ("positive", "negative", "hard_negative") else None,
        "false_accept": fired and kind != "positive",
        "false_reject": (not fired) and kind == "positive",
    }


def _summarize(rows: list[dict], *, kind_filter: str | None = None) -> dict[str, Any]:
    subset = [r for r in rows if kind_filter is None or r["kind"] == kind_filter]
    pos = [r for r in subset if r["kind"] == "positive"]
    neg = [r for r in subset if r["kind"] in ("negative", "hard_negative")]
    return {
        "n": len(subset),
        "positives_fired": sum(1 for r in pos if r["fired"]),
        "positives_total": len(pos),
        "negatives_fired": sum(1 for r in neg if r["fired"]),
        "negatives_total": len(neg),
        "false_accepts": sum(1 for r in neg if r["fired"]),
        "false_rejects": sum(1 for r in pos if not r["fired"]),
        "pos_recall": round(sum(1 for r in pos if r["fired"]) / len(pos), 3) if pos else None,
        "neg_rejection": round(sum(1 for r in neg if not r["fired"]) / len(neg), 3) if neg else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate wake specificity fixes offline")
    parser.add_argument("--hard-negs", action="store_true", help="Include hard-negative session")
    args = parser.parse_args()

    active, thr, personal = resolve_personal_wake()
    threshold = float(thr or 0.05)
    generic_wake, generic_neg = load_generic_references()
    embedder = OwwOnnxEmbedder()

    samples = _load_samples()
    hard_paths: list[Path] = []
    if args.hard_negs:
        hard_samples = _load_hard_negs()
        samples.extend(hard_samples)
        hard_paths = [p for _, _, p in hard_samples]

    hard_neg_matrix = _build_hard_neg_matrix(hard_paths, embedder)

    rules = ["current", "whisper_gate", "hard_neg_contrast", "centroid_only"]
    all_rows: list[dict] = []
    for rule in rules:
        for label, kind, path in samples:
            pcm = _read_pcm(path)
            all_rows.append(
                evaluate_rule(
                    label=label,
                    kind=kind,
                    pcm=pcm,
                    threshold=threshold,
                    generic_wake=generic_wake,
                    generic_neg=generic_neg,
                    personal=personal,
                    embedder=embedder,
                    hard_neg_matrix=hard_neg_matrix,
                    rule=rule,
                )
            )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "threshold": threshold,
        "personal_active": active,
        "hard_negatives_included": bool(hard_paths),
        "live_log_mamacita_evidence": {
            "timestamp": "2026-08-23 14:25:10",
            "generic_margin": -0.0122,
            "personal_margin": 0.0596,
            "combined_margin": 0.0596,
            "activation_method": "embedding_strong",
            "component": "PersonalWakeProfile max-sample scoring",
        },
        "comparison": {
            rule: {
                "all": _summarize([r for r in all_rows if r["rule"] == rule]),
                "positives_only": _summarize([r for r in all_rows if r["rule"] == rule], kind_filter="positive"),
            }
            for rule in rules
        },
        "samples": all_rows,
    }

    out = calibration_root() / "wake_specificity_evaluation.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n======== WAKE SPECIFICITY EVALUATION ========")
    print(f"Threshold: {threshold}  Hard negs: {bool(hard_paths)}")
    for rule in rules:
        s = report["comparison"][rule]["all"]
        print(f"\n{rule}:")
        print(f"  pos recall: {s['positives_fired']}/{s['positives_total']}  "
              f"neg rejection: {s['negatives_total']-s['negatives_fired']}/{s['negatives_total']}  "
              f"false_accepts={s['false_accepts']} false_rejects={s['false_rejects']}")
    print("\nWrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
