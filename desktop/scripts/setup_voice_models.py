"""
Download / prepare voice models for AICA desktop (dev machine).

  python desktop/scripts/setup_voice_models.py

Downloads:
  - faster-whisper base.en  -> desktop/voice/models/whisper-base.en
  - openWakeWord backbone   -> desktop/voice/models/openwakeword/ (optional OWW path)
  - Piper en_US-amy-medium  -> desktop/voice/models/piper/ (IRA neural TTS; ~60 MB, not in Git)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop.launcher.voice_paths import oww_resource_dir, voice_models_dir, whisper_model_dir


def download_whisper() -> None:
    dest = voice_models_dir() / "whisper-base.en"
    if (dest / "model.bin").is_file():
        print("OK whisper already at", dest)
        return
    print("Downloading faster-whisper base.en ->", dest)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(
            repo_id="Systran/faster-whisper-base.en",
            local_dir=str(dest),
            local_dir_use_symlinks=False,
        )
    except Exception as e:
        print("huggingface_hub failed, trying faster_whisper utility:", e)
        from faster_whisper import WhisperModel

        WhisperModel("base.en", device="cpu", compute_type="int8", download_root=str(voice_models_dir()))
    print("OK whisper")


def download_oww_backbone() -> None:
    from desktop.launcher.voice_wake_util import ensure_oww_backbone

    ensure_oww_backbone()
    print("OK openWakeWord backbone at", oww_resource_dir())


def download_piper_amy() -> None:
    from desktop.scripts.setup_piper_voice import main as setup_piper

    code = setup_piper()
    if code != 0:
        raise RuntimeError(f"setup_piper_voice failed with code {code}")


def build_wake_references() -> None:
    from desktop.scripts.train_hey_ira_wake import build_embeddings
    import json
    import numpy as np

    from desktop.launcher.voice_paths import wake_model_path

    wake_vecs, neg_vecs, wake_c, neg_c, wake_labels, neg_labels = build_embeddings()
    out_npz = voice_models_dir() / "hey_ira_embeddings.npz"
    np.savez_compressed(
        str(out_npz),
        wake_embeddings=wake_vecs,
        negative_embeddings=neg_vecs,
        wake_centroid=wake_c,
        negative_centroid=neg_c,
        wake_labels=np.array(wake_labels, dtype=object),
        negative_labels=np.array(neg_labels, dtype=object),
    )
    meta = {
        "type": "embedding_references",
        "phrase": "hey ira",
        "margin_threshold": 0.02,
        "wake_samples": len(wake_labels),
        "negative_samples": len(neg_labels),
    }
    wake_model_path().with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print("OK hey_ira contrastive embeddings at", out_npz)


def main() -> int:
    voice_models_dir().mkdir(parents=True, exist_ok=True)
    download_whisper()
    download_oww_backbone()
    try:
        download_piper_amy()
    except Exception as e:
        print("WARN Piper Amy download skipped:", e)
        print("  Run: python desktop/scripts/setup_piper_voice.py")
    try:
        build_wake_references()
    except Exception as e:
        print("WARN wake reference build skipped:", e)
    print("DONE voice models at", voice_models_dir())
    print("Wake backend: vad_whisper (bounded clip, not continuous dictation)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
