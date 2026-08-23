"""
Build persistent hard-negative wake embedding profile from a collection session.

Usage:
  python desktop/scripts/build_hard_neg_wake.py
  python desktop/scripts/build_hard_neg_wake.py --session "%APPDATA%\\AICA\\logs\\wake_hard_negatives\\hardneg_20260823_143133"

Writes (AppData only — not packaged in AICA.exe):
  %LOCALAPPDATA%\\AICA\\voice\\models\\personal_wake_hard_neg_embeddings.npz
  %LOCALAPPDATA%\\AICA\\voice\\models\\personal_wake_hard_neg.json
"""
from __future__ import annotations

import argparse
import audioop
import json
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop.launcher.voice_paths import hard_neg_wake_meta_path, hard_neg_wake_npz_path
from desktop.launcher.voice_wake_personal import HardNegWakeProfile, _normalize
from desktop.launcher.voice_wake_preprocess import prepare_production_wake_pcm
from desktop.launcher.voice_wake_util import OwwOnnxEmbedder, ensure_oww_backbone

DEFAULT_SESSION = Path(
    r"C:\Users\Samrudh\AppData\Roaming\AICA\logs\wake_hard_negatives\hardneg_20260823_143133"
)


def _read_pcm(wav: Path) -> bytes:
    with wave.open(str(wav), "rb") as wf:
        rate = wf.getframerate()
        width = wf.getsampwidth()
        pcm = wf.readframes(wf.getnframes())
        if width != 2:
            pcm = audioop.lin2lin(pcm, width, 2)
        if wf.getnchannels() == 2:
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        if rate != 16000:
            pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 16000, None)
        return pcm


def build_from_session(session: Path) -> tuple[np.ndarray, list[str]]:
    ensure_oww_backbone()
    embedder = OwwOnnxEmbedder()
    wavs = sorted(session.glob("*.wav"))
    if not wavs:
        raise SystemExit(f"No WAV files in {session}")

    vectors: list[np.ndarray] = []
    labels: list[str] = []
    for wav in wavs:
        pcm = _read_pcm(wav)
        scored_pcm, meta = prepare_production_wake_pcm(pcm)
        if not meta.get("embed_ok"):
            raise ValueError(f"Cannot embed {wav.name}: {meta}")
        audio = np.frombuffer(scored_pcm, dtype=np.int16)
        vec = embedder.embed_audio(audio)
        vectors.append(_normalize(vec))
        labels.append(wav.stem)
    return np.stack(vectors, axis=0).astype(np.float32), labels


def save_profile(
    *,
    embeddings: np.ndarray,
    labels: list[str],
    session_dir: str,
) -> tuple[Path, Path]:
    npz_path = hard_neg_wake_npz_path()
    meta_path = hard_neg_wake_meta_path()
    npz_path.parent.mkdir(parents=True, exist_ok=True)

    if npz_path.is_file():
        bak = npz_path.with_suffix(".npz.bak")
        if not bak.is_file():
            import shutil

            shutil.copy2(npz_path, bak)

    np.savez_compressed(
        str(npz_path),
        hard_neg_embeddings=embeddings,
        hard_neg_labels=np.array(labels, dtype=object),
        session_dir=session_dir,
        source="wake_hard_negatives",
        preprocess_version=HardNegWakeProfile.PREPROCESS_VERSION,
    )

    meta = {
        "type": "hard_neg_wake_profile",
        "session_dir": session_dir,
        "sample_count": len(labels),
        "labels": labels,
        "preprocess_version": HardNegWakeProfile.PREPROCESS_VERSION,
        "preprocess_pipeline": "prepare_production_wake_pcm (VAD + min-length padding + OWW embed)",
        "profile_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "npz_file": npz_path.name,
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return npz_path, meta_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build hard-negative wake embedding profile")
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION)
    args = parser.parse_args()

    session = args.session.resolve()
    if not session.is_dir():
        print("Session not found:", session)
        return 1

    print(f"Building hard-negative profile from {session}")
    embeddings, labels = build_from_session(session)
    npz_path, meta_path = save_profile(
        embeddings=embeddings,
        labels=labels,
        session_dir=str(session),
    )
    print("OK — hard_neg_embeddings:", embeddings.shape)
    print("OK — npz ->", npz_path)
    print("OK — meta ->", meta_path)
    print("Labels:", labels)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
