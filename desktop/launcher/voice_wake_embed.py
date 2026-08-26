"""Lightweight Hey Ira wake via contrastive openWakeWord embeddings (no Whisper loop)."""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Callable

import numpy as np

from desktop.launcher.voice_audio import FRAME_SAMPLES, VoiceActivityDetector
from desktop.launcher.voice_diag import vdiag
from desktop.launcher.voice_log import vlog
from desktop.launcher.voice_paths import voice_models_dir, wake_model_path
from desktop.launcher.voice_wake_preprocess import prepare_production_wake_pcm
from desktop.launcher.voice_wake_util import OwwOnnxEmbedder, ensure_oww_backbone


def _meta_path():
    return wake_model_path().with_suffix(".json")


def _embeddings_path():
    return voice_models_dir() / "hey_ira_embeddings.npz"


class EmbeddingWakeDetector:
    """
    Mic → VAD → short speech clip → OWW embedding → contrastive wake margin.

    score = cos(query, wake_centroid) - cos(query, negative_centroid)

    Strong embedding hits fire on the audio path (no Whisper).
    Ambiguous candidates enqueue Whisper verification on a worker thread —
    the PortAudio callback never waits for Whisper.
    """

    def __init__(self, margin_threshold: float | None = None) -> None:
        self._margin_threshold = margin_threshold
        self._wake_centroid: np.ndarray | None = None
        self._neg_centroid: np.ndarray | None = None
        self._embedder: OwwOnnxEmbedder | None = None
        self._personal = None  # PersonalWakeProfile | None — lazy
        self._hard_neg = None  # HardNegWakeProfile | None — lazy
        self._personal_enabled = False
        self._vad = VoiceActivityDetector(aggressiveness=1)
        self._cooldown_until = 0.0
        self._verify_guard = threading.Lock()
        self._verify_in_flight = False
        self._verify_thread: threading.Thread | None = None
        self._verify_paused = threading.Event()  # set => do not enqueue new jobs

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

        from desktop.launcher.voice_wake_personal import resolve_hard_neg_wake, resolve_personal_wake

        self._personal_enabled = False
        self._personal = None
        self._hard_neg = None
        try:
            hn_active, hard_neg = resolve_hard_neg_wake()
            if hn_active and hard_neg is not None:
                self._hard_neg = hard_neg
                vlog("wake_hard_neg_loaded", samples=hard_neg.meta.get("sample_count"))
        except Exception as e:
            vlog("wake_hard_neg_load_error", error=str(e))

        try:
            active, personal_thr, profile = resolve_personal_wake()
            if active and profile is not None:
                self._personal = profile
                self._personal_enabled = True
                scoring_v = int(profile.meta.get("scoring_version") or 0)
                if self._hard_neg is not None or scoring_v >= 2:
                    from desktop.launcher.voice_wake_personal import PERSONAL_WAKE_STRONG_THRESHOLD_V2

                    self._margin_threshold = PERSONAL_WAKE_STRONG_THRESHOLD_V2
                elif personal_thr is not None:
                    self._margin_threshold = float(personal_thr)
                vlog(
                    "wake_personal_loaded",
                    samples=profile.meta.get("sample_count"),
                    session=profile.meta.get("session_id"),
                    threshold=self._margin_threshold,
                    hard_neg=self._hard_neg is not None,
                )
            else:
                vlog("wake_personal_inactive", reason="disabled_or_missing")
        except Exception as e:
            vlog("wake_personal_load_error", error=str(e))
            self._personal_enabled = False
            self._personal = None

        vlog(
            "wake_embed_loaded",
            margin_threshold=self._margin_threshold,
            personal=self._personal_enabled,
        )

    def pause_verify(self) -> None:
        """Stop accepting new wake Whisper jobs (click-to-talk about to start)."""
        self._verify_paused.set()

    def resume_verify(self) -> None:
        self._verify_paused.clear()

    def wait_verify_idle(self, timeout: float = 45.0) -> bool:
        """Wait for any in-flight wake Whisper worker to finish."""
        thr = self._verify_thread
        if thr is None or not thr.is_alive():
            return True
        thr.join(timeout=timeout)
        return not thr.is_alive()

    def _score_pcm(self, pcm: bytes) -> tuple[float, float, float]:
        if self._embedder is None or self._wake_centroid is None or self._neg_centroid is None:
            self.ensure_loaded()
        audio = np.frombuffer(pcm, dtype=np.int16)
        if audio.size < FRAME_SAMPLES * 4:
            return 0.0, 0.0, -1.0

        scored_pcm, prep_meta = prepare_production_wake_pcm(pcm)
        if not prep_meta.get("embed_ok"):
            return 0.0, 0.0, -1.0
        if prep_meta.get("preprocess") == "padded_center":
            vdiag("WAKE_PCM_PADDED", input_s=round(len(pcm) / 32000.0, 3))

        audio = np.frombuffer(scored_pcm, dtype=np.int16)
        vec = self._embedder.embed_audio(audio)
        vec /= float(np.linalg.norm(vec)) + 1e-9
        wake_sim = float(vec @ self._wake_centroid)
        neg_sim = float(vec @ self._neg_centroid)
        margin = wake_sim - neg_sim

        if self._personal_enabled and self._personal is not None and self._neg_centroid is not None:
            try:
                p_wake, _, p_margin, p_meta = self._personal.score_margin_from_prepared(
                    scored_pcm,
                    neg_centroid=self._neg_centroid,
                    embedder=self._embedder,
                    hard_neg=self._hard_neg,
                )
                if p_meta.get("embed_ok", True) and p_margin > -1.0:
                    vdiag(
                        "WAKE_PERSONAL_SCORE",
                        generic=round(margin, 4),
                        personal=round(p_margin, 4),
                        hard_neg=p_meta.get("hard_neg_sim"),
                    )
                    wake_sim = p_wake
                    neg_sim = float(p_meta.get("neg_sim", neg_sim))
                    margin = p_margin
            except Exception as e:
                vlog("wake_personal_score_error", error=str(e))

        return wake_sim, neg_sim, margin

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
        # Keep ~300 ms of pre-speech audio so the onset of "Hey" is not clipped.
        pre_roll: deque[bytes] = deque(maxlen=10)

        def handle_frame(pcm: bytes) -> None:
            nonlocal in_speech, speech_started, silence_frames
            if time.time() < self._cooldown_until:
                pre_roll.clear()
                return

            if self._vad.is_speech(pcm):
                if not in_speech:
                    in_speech = True
                    speech_started = time.time()
                    pcm_buffer.clear()
                    for prior in pre_roll:
                        pcm_buffer.extend(prior)
                    pre_roll.clear()
                    silence_frames = 0
                pcm_buffer.extend(pcm)
                silence_frames = 0
                if time.time() - speech_started > max_speech_s:
                    self._maybe_fire(bytes(pcm_buffer), on_wake)
                    in_speech = False
                    pcm_buffer.clear()
                return

            if not in_speech:
                pre_roll.append(pcm)
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
        """
        Audio-callback-safe path: embedding score only.
        Strong hit → wake immediately. Ambiguous → enqueue Whisper worker.
        """
        try:
            from desktop.launcher.voice_wake_verify import (
                AMBIGUOUS_MARGIN_LOW,
                MAX_WHISPER_VERIFY_DURATION_S,
                pcm_duration_s,
            )

            wake_sim, neg_sim, margin = self._score_pcm(pcm)
            threshold = float(self._margin_threshold or 0.02)
            duration = pcm_duration_s(pcm)
            vlog(
                "wake_embed_score",
                wake=round(wake_sim, 4),
                neg=round(neg_sim, 4),
                margin=round(margin, 4),
                threshold=threshold,
            )

            if margin >= threshold:
                vlog("wake_strong_embed", margin=round(margin, 4), duration_s=round(duration, 2))
                score = max(0.0, min(1.0, 0.5 + margin * 5.0))
                self._cooldown_until = time.time() + 2.5
                vlog("wake_fired", method="embedding", score=round(score, 3))
                on_wake(score)
                return

            # Ambiguous short clip → offload Whisper; never block the callback.
            if duration > MAX_WHISPER_VERIFY_DURATION_S:
                return
            if margin < AMBIGUOUS_MARGIN_LOW:
                return
            self._enqueue_whisper_verify(pcm, on_wake, margin)
        except Exception as e:
            vlog("wake_embed_error", error=str(e))

    def _enqueue_whisper_verify(
        self,
        pcm: bytes,
        on_wake: Callable[[float], None],
        margin: float,
    ) -> None:
        if self._verify_paused.is_set():
            vdiag("WAKE_VERIFY_SKIPPED", reason="paused")
            return
        with self._verify_guard:
            if self._verify_in_flight:
                vdiag("WAKE_VERIFY_SKIPPED", reason="in_flight")
                return
            self._verify_in_flight = True

        vdiag(
            "WAKE_VERIFY_ENQUEUED",
            margin=round(margin, 4),
            pcm_bytes=len(pcm),
            thread=threading.current_thread().name,
        )

        def worker() -> None:
            try:
                vdiag("WAKE_VERIFY_STARTED", margin=round(margin, 4))
                from desktop.launcher.voice_wake_verify import try_wake_whisper_only

                fired, score, method, wx = try_wake_whisper_only(pcm, margin=margin)
                vdiag(
                    "WAKE_VERIFY_RETURN",
                    fired=fired,
                    method=method,
                    text=wx or "",
                    score=round(score, 3) if score is not None else None,
                )
                if fired:
                    vlog(
                        "wake_fired",
                        method=method,
                        score=round(score, 3),
                        verify_text=wx or None,
                    )
                    self._cooldown_until = time.time() + 2.5
                    if not self._verify_paused.is_set():
                        on_wake(score)
            except Exception as e:
                vlog("wake_whisper_verify_error", error=str(e))
                vdiag("WAKE_VERIFY_ERROR", error=str(e))
            finally:
                with self._verify_guard:
                    self._verify_in_flight = False
                vdiag("WAKE_VERIFY_FINISHED")

        self._verify_thread = threading.Thread(
            target=worker,
            name="aica-wake-verify",
            daemon=True,
        )
        self._verify_thread.start()
