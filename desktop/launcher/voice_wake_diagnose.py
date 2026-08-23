"""Detailed wake activation trace — diagnostics only, no production changes."""
from __future__ import annotations

from typing import Any

from desktop.launcher.voice_wake_personal import (
    PersonalWakeProfile,
    load_generic_references,
    resolve_hard_neg_wake,
    resolve_personal_wake,
    score_combined_margin,
    score_generic_margin,
)
from desktop.launcher.voice_wake_preprocess import ensure_embeddable_pcm, pcm_duration_s
from desktop.launcher.voice_wake_util import OwwOnnxEmbedder
from desktop.launcher.voice_wake_verify import (
    AMBIGUOUS_MARGIN_LOW,
    STRONG_MARGIN_DEFAULT,
    pcm_duration_s as verify_pcm_duration_s,
    try_wake_on_pcm,
    try_wake_whisper_only,
    verify_wake_transcript,
)


def trace_wake_activation(pcm: bytes, *, run_whisper: bool = True) -> dict[str, Any]:
    """
    Full production-equivalent activation trace for one PCM clip (live VAD buffer or WAV).

    Returns per-component scores and which path would fire.
    """
    embedder = OwwOnnxEmbedder()
    generic_wake, generic_neg = load_generic_references()
    active, personal_thr, personal = resolve_personal_wake()
    _, hard_neg = resolve_hard_neg_wake()
    scoring_v = int(personal.meta.get("scoring_version") or 0) if personal else 0
    if hard_neg is not None or scoring_v >= 2:
        from desktop.launcher.voice_wake_personal import PERSONAL_WAKE_STRONG_THRESHOLD_V2

        threshold = PERSONAL_WAKE_STRONG_THRESHOLD_V2
    else:
        threshold = float(personal_thr if personal_thr is not None else STRONG_MARGIN_DEFAULT)

    # Generic (no personal)
    g_wake, g_neg, g_margin, g_meta = score_generic_margin(
        pcm,
        wake_centroid=generic_wake,
        neg_centroid=generic_neg,
        embedder=embedder,
        use_padding=True,
    )

    personal_block: dict[str, Any] | None = None
    combined_margin = g_margin
    combined_wake = g_wake
    combined_neg = g_neg
    winning_path = "generic"

    if active and personal is not None:
        scored = score_combined_margin(
            pcm,
            generic_wake=generic_wake,
            generic_neg=generic_neg,
            personal=personal,
            embedder=embedder,
            hard_neg=hard_neg,
            personal_primary=True,
        )
        personal_block = scored["personal"]
        combined_margin = scored["combined"]["margin"]
        combined_wake = scored["combined"]["wake_sim"]
        combined_neg = scored["combined"]["neg_sim"]
        winning_path = scored["combined"].get("winning_path", "personal")

    scored_pcm, prep = ensure_embeddable_pcm(pcm)
    duration = pcm_duration_s(pcm)

    # Activation decision (mirrors EmbeddingWakeDetector._maybe_fire + try_wake_on_pcm)
    activation_method = "none"
    would_fire = False
    whisper_text = ""
    whisper_would_fire = False
    reason = "below_ambiguous_band"

    if combined_margin >= threshold:
        would_fire = True
        activation_method = "embedding_strong"
        reason = f"combined margin {combined_margin:.4f} >= threshold {threshold}"
    elif duration > 2.8:
        reason = "clip_too_long_for_whisper_verify"
    elif combined_margin < AMBIGUOUS_MARGIN_LOW:
        reason = f"combined margin {combined_margin:.4f} < ambiguous_low {AMBIGUOUS_MARGIN_LOW}"
    elif run_whisper:
        activation_method = "ambiguous_whisper_candidate"
        reason = f"{AMBIGUOUS_MARGIN_LOW} <= margin {combined_margin:.4f} < threshold {threshold}"
        fired, _, method, wx = try_wake_whisper_only(scored_pcm, margin=combined_margin)
        whisper_text = wx or ""
        whisper_would_fire = fired
        if fired:
            would_fire = True
            activation_method = "whisper_verify"
            reason = f"Whisper transcript matched wake: {wx!r}"

    return {
        "input_duration_s": round(duration, 3),
        "preprocess": prep,
        "scored_duration_s": round(pcm_duration_s(scored_pcm), 3),
        "threshold": threshold,
        "ambiguous_low": AMBIGUOUS_MARGIN_LOW,
        "personal_profile_active": active,
        "generic": {
            "wake_sim": round(g_wake, 4),
            "neg_sim": round(g_neg, 4),
            "margin": round(g_margin, 4),
            "preprocess": g_meta.get("preprocess"),
        },
        "personal": None
        if personal_block is None
        else {
            "wake_sim": round(personal_block["wake_sim"], 4),
            "neg_sim": round(personal_block["neg_sim"], 4),
            "margin": round(personal_block["margin"], 4),
            "centroid_sim": personal_block["meta"].get("centroid_sim"),
            "max_sample_sim": personal_block["meta"].get("max_sample_sim"),
            "generic_neg_sim": personal_block["meta"].get("generic_neg_sim"),
            "hard_neg_sim": personal_block["meta"].get("hard_neg_sim"),
        },
        "combined": {
            "wake_sim": round(combined_wake, 4),
            "neg_sim": round(combined_neg, 4),
            "margin": round(combined_margin, 4),
            "winning_path": winning_path,
        },
        "decision": {
            "would_fire": would_fire,
            "activation_method": activation_method,
            "reason": reason,
            "whisper_transcript": whisper_text,
            "whisper_would_fire": whisper_would_fire,
            "detect_wake_on_transcript": verify_wake_transcript(whisper_text, allow_here_mishear=False)
            if whisper_text
            else False,
        },
    }


def trace_via_production_detector(pcm: bytes) -> dict[str, Any]:
    """Use WakeDetector + try_wake_on_pcm — matches evaluate_wake_pcm."""
    from desktop.launcher.voice_wake import WakeDetector
    from desktop.launcher.voice_wake_embed import EmbeddingWakeDetector

    det = WakeDetector()
    det.ensure_loaded()
    if not isinstance(det._backend, EmbeddingWakeDetector):
        return {"error": "not_embedding_backend", "backend": det._mode}

    backend = det._backend
    fired, score, method, wx = try_wake_on_pcm(
        pcm,
        backend._score_pcm,
        strong_margin=float(backend._margin_threshold or STRONG_MARGIN_DEFAULT),
    )
    trace = trace_wake_activation(pcm, run_whisper=False)
    trace["production_try_wake_on_pcm"] = {
        "fired": fired,
        "method": method,
        "score": score,
        "whisper_text": wx,
    }
    return trace
