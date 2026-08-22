"""faster-whisper STT — lazy-loaded, CPU int8, kept in memory."""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any

import numpy as np

from desktop.launcher.voice_diag import vdiag
from desktop.launcher.voice_log import vlog
from desktop.launcher.voice_paths import faster_whisper_assets_dir, whisper_model_dir

# Bias English assistant commands on short/noisy mic clips (small.en).
_EN_INITIAL_PROMPT = (
    "Hey Ira. Open expenses, sales, dashboard, inventory, billing, reports, and analytics. "
    "Take me to sales. Switch to POS or organization."
)


class WhisperSTT:
    _instance: WhisperSTT | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._model = None
        self._model_lock = threading.Lock()
        self._load_error: str | None = None
        self._load_seconds: float | None = None
        self._model_path: str | None = None
        self._pcm_cache: dict[int, dict[str, Any]] = {}
        self._warmed = False

    @classmethod
    def get(cls) -> WhisperSTT:
        with cls._lock:
            if cls._instance is None:
                cls._instance = WhisperSTT()
            return cls._instance

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def load_seconds(self) -> float | None:
        return self._load_seconds

    def _configure_threads(self) -> int:
        threads = int(os.environ.get("AICA_WHISPER_THREADS", "0") or "0")
        if getattr(sys, "frozen", False):
            threads = max(1, int(os.environ.get("AICA_WHISPER_THREADS", "1") or "1"))
        elif threads <= 0:
            threads = max(2, (os.cpu_count() or 4) - 1)
        os.environ["OMP_NUM_THREADS"] = str(threads)
        os.environ["MKL_NUM_THREADS"] = str(threads)
        os.environ["OPENBLAS_NUM_THREADS"] = str(threads)
        os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
        os.environ.setdefault("CT2_USE_EXPERIMENTAL_PACKED_GEMM", "0")
        return threads

    def ensure_loaded(self) -> None:
        if self._model is not None:
            return
        with self._model_lock:
            if self._model is not None:
                return
            t0 = time.perf_counter()
            model_path = whisper_model_dir()
            vdiag(
                "WHISPER_LOAD_START",
                model_path=str(model_path),
                model_bin_exists=(model_path / "model.bin").is_file(),
                meipass=getattr(sys, "_MEIPASS", None),
            )
            if not model_path.is_dir():
                raise FileNotFoundError(
                    f"Whisper model not found at {model_path}. "
                    "Run: python desktop/scripts/setup_voice_models.py"
                )
            try:
                from faster_whisper import WhisperModel

                threads = self._configure_threads()
                vad_asset = faster_whisper_assets_dir() / "silero_vad_v6.onnx"
                vdiag(
                    "WHISPER_LOAD_ENV",
                    threads=threads,
                    vad_asset=str(vad_asset),
                    vad_asset_exists=vad_asset.is_file(),
                    cwd=os.getcwd(),
                )
                self._model = WhisperModel(
                    str(model_path),
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=threads,
                )
                self._model_path = str(model_path)
                self._load_seconds = time.perf_counter() - t0
                vdiag("WHISPER_LOAD_RETURN", seconds=round(self._load_seconds, 3))
                vlog("whisper_loaded", path=str(model_path), seconds=round(self._load_seconds, 2))
                self.warm_transcribe()
            except Exception as e:
                self._load_error = str(e)
                vdiag("WHISPER_LOAD_ERROR", error=str(e))
                vlog("whisper_load_failed", error=str(e))
                raise

    @staticmethod
    def pcm16_to_float(pcm: bytes) -> np.ndarray:
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        return arr / 32768.0

    def warm_transcribe(self) -> None:
        """Prime ctranslate2 after load so first real command is faster."""
        if self._warmed or self._model is None:
            return
        silence = (np.zeros(1600, dtype=np.float32) * 0.001).astype(np.float32)
        try:
            list(
                self._model.transcribe(
                    silence,
                    language="en",
                    beam_size=1,
                    vad_filter=False,
                    condition_on_previous_text=False,
                )[0]
            )
            self._warmed = True
            vdiag("WHISPER_WARM_OK")
        except Exception as e:
            vdiag("WHISPER_WARM_ERROR", error=str(e))

    def transcribe_pcm(
        self,
        pcm: bytes,
        *,
        language: str = "en",
        vad_filter: bool | None = None,
        use_cache: bool = True,
        normalize: bool = True,
    ) -> dict[str, Any]:
        self.ensure_loaded()
        if not pcm:
            return {"text": "", "confidence": None, "latency_ms": 0.0, "segments": []}

        cache_key = hash(pcm) if use_cache else None
        if cache_key is not None and cache_key in self._pcm_cache:
            cached = dict(self._pcm_cache[cache_key])
            cached["latency_ms"] = 0.0
            cached["cached"] = True
            vdiag("WHISPER_TRANSCRIBE_CACHE_HIT", pcm_bytes=len(pcm))
            return cached

        if vad_filter is None:
            # We already VAD-gate mic input; silero in faster-whisper is redundant and
            # requires silero_vad_v6.onnx on disk (must be bundled for PyInstaller).
            vad_filter = False

        if normalize:
            from desktop.launcher.voice_audio import normalize_pcm_rms

            pcm = normalize_pcm_rms(pcm)

        audio = self.pcm16_to_float(pcm)
        samples = len(audio)
        duration_s = round(samples / 16000.0, 3)
        beam_size = max(1, int(os.environ.get("AICA_WHISPER_BEAM_SIZE", "3") or "3"))
        vdiag(
            "WHISPER_TRANSCRIBE_START",
            pcm_bytes=len(pcm),
            samples=samples,
            duration_s=duration_s,
            model_path=self._model_path,
            vad_filter=vad_filter,
            beam_size=beam_size,
        )
        t0 = time.perf_counter()
        try:
            segments, info = self._model.transcribe(
                audio,
                language=language,
                task="transcribe",
                beam_size=beam_size,
                best_of=1,
                vad_filter=vad_filter,
                condition_on_previous_text=False,
                initial_prompt=_EN_INITIAL_PROMPT,
                temperature=0.0,
            )
            vdiag("WHISPER_TRANSCRIBE_GENERATOR", elapsed_ms=round((time.perf_counter() - t0) * 1000, 1))
        except Exception as e:
            vdiag("WHISPER_TRANSCRIBE_ERROR", error=str(e))
            raise

        parts: list[str] = []
        seg_out: list[dict[str, Any]] = []
        avg_logprob = []
        seg_count = 0
        for seg in segments:
            seg_count += 1
            t = (seg.text or "").strip()
            if t:
                parts.append(t)
            seg_out.append(
                {
                    "text": t,
                    "start": seg.start,
                    "end": seg.end,
                    "avg_logprob": getattr(seg, "avg_logprob", None),
                }
            )
            if getattr(seg, "avg_logprob", None) is not None:
                avg_logprob.append(float(seg.avg_logprob))

        text = " ".join(parts).strip()
        latency_ms = (time.perf_counter() - t0) * 1000.0
        confidence = None
        if avg_logprob:
            confidence = max(0.0, min(1.0, 1.0 + (sum(avg_logprob) / len(avg_logprob))))

        vdiag(
            "WHISPER_TRANSCRIBE_RETURN",
            segments=seg_count,
            text=text,
            latency_ms=round(latency_ms, 1),
            language=getattr(info, "language", language),
        )
        result = {
            "text": text,
            "confidence": confidence,
            "latency_ms": latency_ms,
            "language": getattr(info, "language", language),
            "segments": seg_out,
            "cached": False,
        }
        if cache_key is not None:
            self._pcm_cache[cache_key] = dict(result)
            if len(self._pcm_cache) > 8:
                self._pcm_cache.pop(next(iter(self._pcm_cache)))
        return result
