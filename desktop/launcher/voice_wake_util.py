"""openWakeWord backbone ONNX helpers (no openwakeword package import — PyInstaller-safe)."""
from __future__ import annotations

import urllib.request
from pathlib import Path

import numpy as np

from desktop.launcher.voice_log import vlog
from desktop.launcher.voice_paths import oww_resource_dir

OWW_MEL_URL = (
    "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.onnx"
)
OWW_EMB_URL = (
    "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.onnx"
)


def _download(url: str, dest: Path) -> None:
    if dest.is_file() and dest.stat().st_size > 1000:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    vlog("oww_download", url=url, dest=str(dest))
    urllib.request.urlretrieve(url, dest)


def ensure_oww_backbone() -> None:
    base = oww_resource_dir()
    _download(OWW_MEL_URL, base / "melspectrogram.onnx")
    _download(OWW_EMB_URL, base / "embedding_model.onnx")


class OwwOnnxEmbedder:
    """Minimal mel + embedding inference using bundled ONNX models."""

    def __init__(self) -> None:
        ensure_oww_backbone()
        import onnxruntime as ort

        res = oww_resource_dir()
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._mel = ort.InferenceSession(
            str(res / "melspectrogram.onnx"),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self._emb = ort.InferenceSession(
            str(res / "embedding_model.onnx"),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )

    @staticmethod
    def _melspec_transform(spec: np.ndarray) -> np.ndarray:
        return spec / 10.0 + 2.0

    def _melspectrogram(self, audio: np.ndarray) -> np.ndarray:
        x = np.asarray(audio, dtype=np.int16)
        if x.ndim == 1:
            x = x[None, :]
        x = x.astype(np.float32)
        spec = np.squeeze(self._mel.run(None, {"input": x})[0])
        return self._melspec_transform(spec)

    def embed_audio(self, audio: np.ndarray) -> np.ndarray:
        """Return mean speech embedding vector for int16 mono 16 kHz audio."""
        spec = self._melspectrogram(audio)
        windows: list[np.ndarray] = []
        window_size = 76
        step = 8
        for i in range(0, spec.shape[0], step):
            window = spec[i : i + window_size]
            if window.shape[0] == window_size:
                windows.append(window)
        if not windows:
            raise ValueError("Audio too short for embedding")
        batch = np.expand_dims(np.array(windows), axis=-1).astype(np.float32)
        emb = self._emb.run(None, {"input_1": batch})[0].squeeze()
        if emb.ndim == 1:
            return emb.astype(np.float32)
        return emb.mean(axis=0).astype(np.float32)

    def embed_pcm16(self, pcm: bytes) -> np.ndarray:
        audio = np.frombuffer(pcm, dtype=np.int16)
        return self.embed_audio(audio)
