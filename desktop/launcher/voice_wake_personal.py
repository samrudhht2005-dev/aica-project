"""Personal Hey IRA wake profile — load, score, and combine with generic references."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from desktop.launcher.voice_paths import (
    hard_neg_wake_meta_path,
    hard_neg_wake_npz_path,
    personal_wake_meta_path,
    personal_wake_npz_path,
    voice_models_dir,
)
from desktop.launcher.voice_wake_preprocess import prepare_production_wake_pcm
from desktop.launcher.voice_wake_util import OwwOnnxEmbedder, ensure_oww_backbone


def _env_force_personal_wake() -> bool | None:
    """Optional override: 0=off, 1=on, unset=use metadata."""
    raw = os.environ.get("AICA_PERSONAL_WAKE", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return None


def personal_wake_enabled() -> bool:
    """True when personal profile metadata marks it enabled and NPZ validates."""
    return resolve_personal_wake()[0]


def resolve_personal_wake() -> tuple[bool, float | None, "PersonalWakeProfile | None"]:
    """
    Production activation: personal_hey_ira.json enabled:true + valid NPZ.
    Returns (active, margin_threshold, profile).
    Falls back to generic-only when missing/invalid — never raises.
    """
    forced = _env_force_personal_wake()
    meta_path = personal_wake_meta_path()
    npz_path = personal_wake_npz_path()

    if forced is False:
        return False, None, None
    if not meta_path.is_file() or not npz_path.is_file():
        return False, None, None

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False, None, None

    if meta.get("type") != "personal_wake_profile":
        return False, None, None

    enabled_meta = bool(meta.get("enabled"))
    if forced is not True and not enabled_meta:
        return False, None, None

    profile = PersonalWakeProfile()
    if not profile.ensure_loaded():
        return False, None, None
    if not profile.validate():
        return False, None, None

    thr = meta.get("margin_threshold")
    if thr is None:
        thr = meta.get("recommended_threshold")
    threshold = float(thr) if thr is not None else None
    return True, threshold, profile


def _generic_embeddings_path() -> Path:
    return voice_models_dir() / "hey_ira_embeddings.npz"


def _centroid(vectors: np.ndarray) -> np.ndarray:
    normed = vectors.astype(np.float32)
    norms = np.linalg.norm(normed, axis=1, keepdims=True) + 1e-9
    normed = normed / norms
    c = normed.mean(axis=0)
    c /= float(np.linalg.norm(c)) + 1e-9
    return c.astype(np.float32)


def _normalize(vec: np.ndarray) -> np.ndarray:
    v = vec.astype(np.float32)
    v /= float(np.linalg.norm(v)) + 1e-9
    return v


# Personal wake v2: hard-negative contrast + 0.03 strong threshold (see evaluation).
PERSONAL_WAKE_SCORING_V2 = 2
PERSONAL_WAKE_STRONG_THRESHOLD_V2 = 0.03


class HardNegWakeProfile:
    """Persistent targeted hard-negative embeddings for contrastive wake scoring."""

    PREPROCESS_VERSION = "prepare_production_wake_pcm_v1"

    def __init__(self) -> None:
        self._embeddings: np.ndarray | None = None
        self._labels: list[str] = []
        self._meta: dict[str, Any] = {}
        self._loaded = False

    @property
    def meta(self) -> dict[str, Any]:
        return dict(self._meta)

    @property
    def is_loaded(self) -> bool:
        return self._loaded and self._embeddings is not None

    def validate(self) -> bool:
        if self._embeddings is None:
            return False
        if self._embeddings.ndim != 2 or self._embeddings.shape[0] < 1:
            return False
        return True

    def ensure_loaded(self) -> bool:
        if self._loaded:
            return self.validate()
        npz_path = hard_neg_wake_npz_path()
        if not npz_path.is_file():
            return False
        try:
            ensure_oww_backbone()
            data = np.load(str(npz_path), allow_pickle=True)
            if "hard_neg_embeddings" not in data:
                return False
            self._embeddings = np.asarray(data["hard_neg_embeddings"], dtype=np.float32)
            self._labels = [str(x) for x in data.get("hard_neg_labels", []).tolist()]
            meta_path = hard_neg_wake_meta_path()
            if meta_path.is_file():
                self._meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self._loaded = True
            return self.validate()
        except Exception:
            self._loaded = False
            return False

    def max_similarity(self, vec: np.ndarray) -> float:
        if not self.ensure_loaded() or self._embeddings is None:
            return 0.0
        v = _normalize(vec)
        return float(np.max(self._embeddings @ v))


def resolve_hard_neg_wake() -> tuple[bool, HardNegWakeProfile | None]:
    profile = HardNegWakeProfile()
    if profile.ensure_loaded():
        return True, profile
    return False, None


class PersonalWakeProfile:
    """Personal wake embeddings built from user mic calibration samples."""

    def __init__(self) -> None:
        self._wake_embeddings: np.ndarray | None = None
        self._wake_centroid: np.ndarray | None = None
        self._labels: list[str] = []
        self._meta: dict[str, Any] = {}
        self._loaded = False

    @property
    def meta(self) -> dict[str, Any]:
        return dict(self._meta)

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def margin_threshold(self) -> float | None:
        raw = self._meta.get("margin_threshold")
        if raw is None:
            raw = self._meta.get("recommended_threshold")
        return float(raw) if raw is not None else None

    def validate(self) -> bool:
        if self._wake_embeddings is None or self._wake_centroid is None:
            return False
        if self._wake_embeddings.ndim != 2 or self._wake_centroid.ndim != 1:
            return False
        if self._wake_embeddings.shape[1] != self._wake_centroid.shape[0]:
            return False
        if self._wake_embeddings.shape[0] < 1:
            return False
        return True

    def ensure_loaded(self) -> bool:
        if self._loaded:
            return self.validate()
        npz_path = personal_wake_npz_path()
        if not npz_path.is_file():
            return False
        try:
            ensure_oww_backbone()
            data = np.load(str(npz_path), allow_pickle=True)
            if "personal_wake_embeddings" not in data or "personal_wake_centroid" not in data:
                return False
            self._wake_embeddings = np.asarray(data["personal_wake_embeddings"], dtype=np.float32)
            self._wake_centroid = np.asarray(data["personal_wake_centroid"], dtype=np.float32)
            self._labels = [str(x) for x in data.get("personal_labels", []).tolist()]
            meta_path = personal_wake_meta_path()
            if meta_path.is_file():
                self._meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self._loaded = True
            return self.validate()
        except Exception:
            self._loaded = False
            return False

    def score_margin(
        self,
        pcm: bytes,
        *,
        neg_centroid: np.ndarray,
        embedder: OwwOnnxEmbedder | None = None,
        hard_neg: HardNegWakeProfile | None = None,
    ) -> tuple[float, float, float, dict[str, Any]]:
        """
        Personal margin = max(cos(q, centroid), max_i cos(q, sample_i))
                          - max(cos(q, generic_neg), max_j cos(q, hard_neg_j)).
        """
        if not self._loaded:
            self.ensure_loaded()
        if self._wake_centroid is None or self._wake_embeddings is None:
            raise RuntimeError("Personal wake profile not loaded")

        embedder = embedder or OwwOnnxEmbedder()
        scored_pcm, prep_meta = prepare_production_wake_pcm(pcm)
        if not prep_meta["embed_ok"]:
            return 0.0, 0.0, -1.0, prep_meta

        audio = np.frombuffer(scored_pcm, dtype=np.int16)
        vec = _normalize(embedder.embed_audio(audio))
        prep_meta.update(self._score_vec(vec, neg_centroid, hard_neg=hard_neg))
        return prep_meta["wake_sim"], prep_meta["neg_sim"], prep_meta["margin"], prep_meta

    def score_margin_from_prepared(
        self,
        scored_pcm: bytes,
        *,
        neg_centroid: np.ndarray,
        embedder: OwwOnnxEmbedder | None = None,
        hard_neg: HardNegWakeProfile | None = None,
    ) -> tuple[float, float, float, dict[str, Any]]:
        """Score already preprocessed (padded) live wake PCM — skips re-VAD."""
        if not self._loaded:
            self.ensure_loaded()
        if self._wake_centroid is None or self._wake_embeddings is None:
            raise RuntimeError("Personal wake profile not loaded")

        embedder = embedder or OwwOnnxEmbedder()
        audio = np.frombuffer(scored_pcm, dtype=np.int16)
        if audio.size < 1:
            return 0.0, 0.0, -1.0, {"embed_ok": False}
        try:
            vec = _normalize(embedder.embed_audio(audio))
        except ValueError as exc:
            return 0.0, 0.0, -1.0, {"embed_ok": False, "error": str(exc)}

        prep_meta: dict[str, Any] = {"embed_ok": True}
        prep_meta.update(self._score_vec(vec, neg_centroid, hard_neg=hard_neg))
        return prep_meta["wake_sim"], prep_meta["neg_sim"], prep_meta["margin"], prep_meta

    def _score_vec(
        self,
        vec: np.ndarray,
        neg_centroid: np.ndarray,
        *,
        hard_neg: HardNegWakeProfile | None,
    ) -> dict[str, Any]:
        generic_neg_sim = float(vec @ _normalize(neg_centroid))
        hard_neg_sim = hard_neg.max_similarity(vec) if hard_neg is not None else 0.0
        neg_sim = max(generic_neg_sim, hard_neg_sim)
        centroid_sim = float(vec @ _normalize(self._wake_centroid))  # type: ignore[arg-type]
        sample_sims = self._wake_embeddings @ vec  # type: ignore[operator]
        max_sample_sim = float(np.max(sample_sims))
        wake_sim = max(centroid_sim, max_sample_sim)
        margin = wake_sim - neg_sim
        return {
            "wake_sim": round(wake_sim, 4),
            "neg_sim": round(neg_sim, 4),
            "margin": round(margin, 4),
            "centroid_sim": round(centroid_sim, 4),
            "max_sample_sim": round(max_sample_sim, 4),
            "generic_neg_sim": round(generic_neg_sim, 4),
            "hard_neg_sim": round(hard_neg_sim, 4),
        }


def load_generic_references() -> tuple[np.ndarray, np.ndarray]:
    path = _generic_embeddings_path()
    if not path.is_file():
        raise FileNotFoundError(f"Generic wake embeddings missing: {path}")
    data = np.load(str(path), allow_pickle=True)
    wake_c = np.asarray(data["wake_centroid"], dtype=np.float32)
    neg_c = np.asarray(data["negative_centroid"], dtype=np.float32)
    return wake_c, neg_c


def score_generic_margin(
    pcm: bytes,
    *,
    wake_centroid: np.ndarray,
    neg_centroid: np.ndarray,
    embedder: OwwOnnxEmbedder | None = None,
    use_padding: bool = True,
) -> tuple[float, float, float, dict[str, Any]]:
    embedder = embedder or OwwOnnxEmbedder()
    if use_padding:
        scored_pcm, prep_meta = prepare_production_wake_pcm(pcm)
    else:
        from desktop.launcher.voice_wake_preprocess import pcm_duration_s

        prep_meta = {
            "input_duration_s": round(pcm_duration_s(pcm), 3),
            "preprocess": "raw_vad_or_input",
            "embed_ok": True,
        }
        scored_pcm = pcm

    if not prep_meta.get("embed_ok", True):
        return 0.0, 0.0, -1.0, prep_meta

    audio = np.frombuffer(scored_pcm, dtype=np.int16)
    try:
        vec = _normalize(embedder.embed_audio(audio))
    except ValueError as exc:
        prep_meta["embed_ok"] = False
        prep_meta["error"] = str(exc)
        return 0.0, 0.0, -1.0, prep_meta

    wake_sim = float(vec @ _normalize(wake_centroid))
    neg_sim = float(vec @ _normalize(neg_centroid))
    margin = wake_sim - neg_sim
    prep_meta["wake_sim"] = round(wake_sim, 4)
    prep_meta["neg_sim"] = round(neg_sim, 4)
    prep_meta["margin"] = round(margin, 4)
    return wake_sim, neg_sim, margin, prep_meta


def score_combined_margin(
    pcm: bytes,
    *,
    generic_wake: np.ndarray,
    generic_neg: np.ndarray,
    personal: PersonalWakeProfile,
    embedder: OwwOnnxEmbedder | None = None,
    hard_neg: HardNegWakeProfile | None = None,
    personal_primary: bool = True,
) -> dict[str, Any]:
    """
    Score generic + personal wake paths.

    When personal_primary=True (production with personal enabled), the decision
    margin is the personal hard-neg-contrast margin only — generic cannot bypass
    hard-negative protection via max().
    """
    embedder = embedder or OwwOnnxEmbedder()
    g_wake, g_neg, g_margin, g_meta = score_generic_margin(
        pcm,
        wake_centroid=generic_wake,
        neg_centroid=generic_neg,
        embedder=embedder,
        use_padding=True,
    )
    p_wake, p_neg, p_margin, p_meta = personal.score_margin(
        pcm,
        neg_centroid=generic_neg,
        embedder=embedder,
        hard_neg=hard_neg,
    )
    if personal_primary:
        combined_wake = p_wake
        combined_neg = p_neg
        combined_margin = p_margin
        winning_path = "personal"
    else:
        combined_wake = max(g_wake, p_wake)
        combined_neg = g_neg
        combined_margin = max(g_margin, p_margin)
        winning_path = "personal" if p_margin > g_margin else "generic"
    return {
        "generic": {"wake_sim": g_wake, "neg_sim": g_neg, "margin": g_margin, "meta": g_meta},
        "personal": {"wake_sim": p_wake, "neg_sim": p_neg, "margin": p_margin, "meta": p_meta},
        "combined": {
            "wake_sim": combined_wake,
            "neg_sim": combined_neg,
            "margin": combined_margin,
            "winning_path": winning_path,
        },
    }


def build_personal_profile_from_wavs(
    wav_paths: list[tuple[str, Path]],
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Embed positive calibration WAVs with production-equivalent preprocessing."""
    import audioop
    import wave

    ensure_oww_backbone()
    embedder = OwwOnnxEmbedder()
    vectors: list[np.ndarray] = []
    labels: list[str] = []

    for label, wav_path in wav_paths:
        with wave.open(str(wav_path), "rb") as wf:
            rate = wf.getframerate()
            width = wf.getsampwidth()
            pcm = wf.readframes(wf.getnframes())
            if width != 2:
                pcm = audioop.lin2lin(pcm, width, 2)
            if wf.getnchannels() == 2:
                pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
            if rate != 16000:
                pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 16000, None)

        scored_pcm, meta = prepare_production_wake_pcm(pcm)
        if not meta["embed_ok"]:
            raise ValueError(f"Cannot embed {label}: {meta}")
        audio = np.frombuffer(scored_pcm, dtype=np.int16)
        vec = embedder.embed_audio(audio)
        vectors.append(_normalize(vec))
        labels.append(label)

    stacked = np.stack(vectors, axis=0).astype(np.float32)
    centroid = _centroid(stacked)
    return stacked, centroid, labels


def save_personal_profile(
    *,
    embeddings: np.ndarray,
    centroid: np.ndarray,
    labels: list[str],
    session_id: str,
    session_dir: str,
    recommended_threshold: float | None = None,
    backup_existing: bool = True,
) -> tuple[Path, Path]:
    npz_path = personal_wake_npz_path()
    meta_path = personal_wake_meta_path()
    npz_path.parent.mkdir(parents=True, exist_ok=True)

    if backup_existing and npz_path.is_file():
        bak = npz_path.with_suffix(".npz.bak")
        if not bak.is_file():
            import shutil

            shutil.copy2(npz_path, bak)
    if backup_existing and meta_path.is_file():
        bak_meta = meta_path.with_suffix(".json.bak")
        if not bak_meta.is_file():
            import shutil

            shutil.copy2(meta_path, bak_meta)

    thr = recommended_threshold if recommended_threshold is not None else PERSONAL_WAKE_STRONG_THRESHOLD_V2

    prev_enabled = False
    if meta_path.is_file():
        try:
            prev_enabled = bool(json.loads(meta_path.read_text(encoding="utf-8")).get("enabled"))
        except Exception:
            prev_enabled = False

    np.savez_compressed(
        str(npz_path),
        personal_wake_embeddings=embeddings,
        personal_wake_centroid=centroid,
        personal_labels=np.array(labels, dtype=object),
        session_id=session_id,
        source="wake_calibration",
        scoring_version=PERSONAL_WAKE_SCORING_V2,
    )

    hey_count = sum(1 for lb in labels if "hey" in lb.lower())
    hi_count = sum(1 for lb in labels if lb.startswith("pos_hi_"))

    meta = {
        "type": "personal_wake_profile",
        "phrase": "hey ira / hi aira",
        "session_id": session_id,
        "session_dir": session_dir,
        "sample_count": len(labels),
        "hey_aira_count": hey_count,
        "hi_aira_count": hi_count,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "enabled": prev_enabled,
        "margin_threshold": thr,
        "recommended_threshold": thr,
        "scoring_version": PERSONAL_WAKE_SCORING_V2,
        "hard_neg_profile": hard_neg_wake_npz_path().name,
        "production_gate": "Set enabled:true after Phase 2 evaluation approval",
        "scoring": {
            "personal_margin": (
                "max(cos(q, personal_centroid), max_i cos(q, personal_i)) "
                "- max(cos(q, generic_neg), max_j cos(q, hard_neg_j))"
            ),
            "combined_margin": "personal_margin when personal enabled (generic cannot bypass hard-negs)",
            "strong_threshold": thr,
            "ambiguous_low": -0.06,
        },
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return npz_path, meta_path


def update_recommended_threshold(threshold: float) -> None:
    meta_path = personal_wake_meta_path()
    if not meta_path.is_file():
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["recommended_threshold"] = threshold
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
