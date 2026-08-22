"""
Build Hey Ira wake references for contrastive embedding detection (CPU ONNX).

  python desktop/scripts/train_hey_ira_wake.py
"""
from __future__ import annotations

import audioop
import json
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop.launcher.voice_paths import oww_resource_dir, voice_models_dir, wake_model_path
from desktop.launcher.voice_wake_util import OwwOnnxEmbedder, ensure_oww_backbone
from desktop.scripts.setup_voice_models import download_oww_backbone, download_whisper

WAKE_PHRASES = [
    "Hey Ira",
    "Hey, Ira",
    "Hay Ira",
    "Hi Ira",
    "Hey Aira",
    "Hey Era",
    "Hey Ira!",
    "Hey I ra",
    "Hey Ira please",
    "Hey Ira open",
    "He Ira",
    "Hay Aira",
]

NEGATIVE_PHRASES = [
    "Open expenses",
    "Open sales",
    "Open dashboard",
    "Open inventory",
    "Open billing",
    "Switch to POS",
    "Switch to organization",
    "Take me to expenses",
    "Take me to sales",
    "What are my expenses",
    "Hello there",
    "Good morning",
    "Thank you",
    "How are you",
    "Show reports",
]

VOICES = [
    "Microsoft Zira Desktop",
    "Microsoft David Desktop",
    None,
]


def synthesize_wav(text: str, voice_name: str | None, out_path: Path) -> None:
    import clr  # type: ignore

    clr.AddReference(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\WPF\System.Speech.dll")
    from System.IO import FileStream, FileMode  # type: ignore
    from System.Speech.Synthesis import SpeechSynthesizer  # type: ignore

    synth = SpeechSynthesizer()
    stream = FileStream(str(out_path), FileMode.Create)
    try:
        if voice_name:
            try:
                synth.SelectVoice(voice_name)
            except Exception:
                pass
        synth.SetOutputToWaveStream(stream)
        synth.Speak(text)
    finally:
        synth.SetOutputToNull()
        stream.Close()
        synth.Dispose()


def wav_to_pcm16_16k(wav_path: Path) -> bytes:
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
        return pcm


def _audio_features() -> OwwOnnxEmbedder:
    ensure_oww_backbone()
    return OwwOnnxEmbedder()


def _embed_pcm(features: OwwOnnxEmbedder, pcm: bytes) -> np.ndarray:
    return features.embed_pcm16(pcm)


def _embed_phrases(phrases: list[str]) -> tuple[np.ndarray, list[str]]:
    features = _audio_features()
    vectors: list[np.ndarray] = []
    labels: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="hey_ira_emb_"))

    for phrase in phrases:
        for voice in VOICES:
            tag = voice or "default"
            wav = tmp / f"{abs(hash((phrase, tag)))}.wav"
            synthesize_wav(phrase, voice, wav)
            pcm = wav_to_pcm16_16k(wav)
            audio = np.frombuffer(pcm, dtype=np.int16)
            if audio.size < 8000:
                continue
            vectors.append(_embed_pcm(features, pcm))
            labels.append(f"{phrase}|{tag}")

    if not vectors:
        raise RuntimeError(f"No embeddings for phrases: {phrases[:3]}")
    return np.stack(vectors, axis=0), labels


def _centroid(vectors: np.ndarray) -> np.ndarray:
    normed = vectors.astype(np.float32)
    norms = np.linalg.norm(normed, axis=1, keepdims=True) + 1e-9
    normed = normed / norms
    c = normed.mean(axis=0)
    c /= float(np.linalg.norm(c)) + 1e-9
    return c.astype(np.float32)


def build_embeddings() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[str], list[str]]:
    wake_vecs, wake_labels = _embed_phrases(WAKE_PHRASES)
    neg_vecs, neg_labels = _embed_phrases(NEGATIVE_PHRASES)
    wake_centroid = _centroid(wake_vecs)
    neg_centroid = _centroid(neg_vecs)
    return wake_vecs, neg_vecs, wake_centroid, neg_centroid, wake_labels, neg_labels


def main() -> int:
    voice_models_dir().mkdir(parents=True, exist_ok=True)
    download_whisper()
    download_oww_backbone()

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
    meta_path = wake_model_path().with_suffix(".json")
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"OK — wake={wake_vecs.shape[0]} neg={neg_vecs.shape[0]} -> {out_npz}")
    print(f"OK — metadata -> {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
