"""Lightweight Hey Ira wake via contrastive openWakeWord embeddings (no Whisper loop)."""
from __future__ import annotations

import json
import time
from typing import Callable

import numpy as np

from desktop.launcher.voice_audio import FRAME_SAMPLES, VoiceActivityDetector
from desktop.launcher.voice_log import vlog
from desktop.launcher.voice_paths import voice_models_dir, wake_model_path
from desktop.launcher.voice_wake_util import OwwOnnxEmbedder, ensure_oww_backbone


def _meta_path():
    return wake_model_path().with_suffix(".json")


def _embeddings_path():
    return voice_models_dir() / "hey_ira_embeddings.npz"


class EmbeddingWakeDetector:
    """
    Mic → VAD → short speech clip → OWW embedding → contrastive wake margin.

    score = cos(query, wake_centroid) - cos(query, negative_centroid)
    """

    def __init__(self, margin_threshold: float | None = None) -> None:
        self._margin_threshold = margin_threshold
        self._wake_centroid: np.ndarray | None = None
        self._neg_centroid: np.ndarray | None = None
        self._embedder: OwwOnnxEmbedder | None = None
        self._vad = VoiceActivityDetector(aggressiveness=2)
        self._cooldown_until = 0.0

    def ensure_loaded(self) -> None:
        ensure_oww_backbone()
        emb_path = _embeddings_path()
        if not emb_path.is_file():
            raise FileNotFoundError(
                f"Hey Ira embeddings missing at {emb_path}. "
                "Run: python desktop/scripts/train_hey_ira_wake.py"
            )
        data = np.load(str(emb_path))
        self._wake_centroid = np.asarray(data["wake_centroid"], dtype=np.float32)
        self._neg_centroid = np.asarray(data["negative_centroid"], dtype=np.float32)

        meta = {}
        mp = _meta_path()
        if mp.is_file():
            meta = json.loads(mp.read_text(encoding="utf-8"))
        if self._margin_threshold is None:
            self._margin_threshold = float(meta.get("margin_threshold", 0.02))

        self._embedder = OwwOnnxEmbedder()
        vlog("wake_embed_loaded", margin_threshold=self._margin_threshold)

    def _score_pcm(self, pcm: bytes) -> tuple[float, float, float]:
        if self._embedder is None or self._wake_centroid is None or self._neg_centroid is None:
            self.ensure_loaded()
        audio = np.frombuffer(pcm, dtype=np.int16)
        if audio.size < FRAME_SAMPLES * 4:
            return 0.0, 0.0, -1.0
        vec = self._embedder.embed_audio(audio)
        vec /= float(np.linalg.norm(vec)) + 1e-9
        wake_sim = float(vec @ self._wake_centroid)
        neg_sim = float(vec @ self._neg_centroid)
        return wake_sim, neg_sim, wake_sim - neg_sim

    def run_loop(
        self,
        *,
        on_wake: Callable[[float], None],
        stop_check: Callable[[], bool],
        stream_frames: Callable[[Callable[[bytes], None], Callable[[], bool]], None],
    ) -> None:
        pcm_buffer = bytearray()
        in_speech = False
        speech_started = 0.0
        max_speech_s = 2.2
        min_speech_s = 0.28
        silence_frames = 0
        silence_limit = 10

        def handle_frame(pcm: bytes) -> None:
            nonlocal in_speech, speech_started, silence_frames
            if time.time() < self._cooldown_until:
                return

            if self._vad.is_speech(pcm):
                if not in_speech:
                    in_speech = True
                    speech_started = time.time()
                    pcm_buffer.clear()
                    silence_frames = 0
                pcm_buffer.extend(pcm)
                silence_frames = 0
                if time.time() - speech_started > max_speech_s:
                    self._maybe_fire(bytes(pcm_buffer), on_wake)
                    in_speech = False
                    pcm_buffer.clear()
                return

            if not in_speech:
                return

            pcm_buffer.extend(pcm)
            silence_frames += 1
            if silence_frames >= silence_limit:
                if time.time() - speech_started >= min_speech_s:
                    self._maybe_fire(bytes(pcm_buffer), on_wake)
                in_speech = False
                pcm_buffer.clear()
                silence_frames = 0

        stream_frames(frame_callback=handle_frame, stop_check=stop_check)

    def _maybe_fire(self, pcm: bytes, on_wake: Callable[[float], None]) -> None:
        try:
            from desktop.launcher.voice_wake_verify import try_wake_on_pcm

            wake_sim, neg_sim, margin = self._score_pcm(pcm)
            vlog(
                "wake_embed_score",
                wake=round(wake_sim, 4),
                neg=round(neg_sim, 4),
                margin=round(margin, 4),
                threshold=self._margin_threshold,
            )
            fired, score, method, wx = try_wake_on_pcm(
                pcm,
                self._score_pcm,
                strong_margin=float(self._margin_threshold or 0.02),
            )
            if fired:
                vlog("wake_fired", method=method, score=round(score, 3), verify_text=wx or None)
                self._cooldown_until = time.time() + 2.5
                on_wake(score)
        except Exception as e:
            vlog("wake_embed_error", error=str(e))
