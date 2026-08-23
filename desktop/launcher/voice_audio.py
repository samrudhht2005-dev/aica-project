"""Microphone capture + WebRTC VAD for speech boundaries."""
from __future__ import annotations

import collections
import threading
import time
from typing import Callable

import numpy as np

from desktop.launcher.voice_diag import vdiag
from desktop.launcher.voice_log import vlog

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_MS / 1000)
BYTES_PER_SAMPLE = 2


def pcm_duration_ms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    return len(pcm) / (SAMPLE_RATE * BYTES_PER_SAMPLE) * 1000.0


def normalize_pcm_rms(pcm: bytes, *, target_rms: float = 4500.0, max_gain: float = 12.0) -> bytes:
    """Boost quiet mic clips before Whisper without clipping."""
    if not pcm or len(pcm) < 4:
        return pcm
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    rms = float(np.sqrt(np.mean(arr * arr)))
    if rms < 120.0:
        return pcm
    gain = min(max_gain, target_rms / rms)
    if abs(gain - 1.0) < 0.05:
        return pcm
    boosted = np.clip(arr * gain, -32768, 32767).astype(np.int16)
    return boosted.tobytes()


class VoiceActivityDetector:
    """Energy-threshold VAD (no native compiler required; PyInstaller-safe)."""

    def __init__(self, aggressiveness: int = 2) -> None:
        # Map 0-3 aggressiveness to RMS threshold on int16 PCM
        thresholds = {0: 350.0, 1: 500.0, 2: 700.0, 3: 950.0}
        self._threshold = thresholds.get(int(aggressiveness), 700.0)
        self.sample_rate = SAMPLE_RATE

    def is_speech(self, frame_pcm16: bytes) -> bool:
        if len(frame_pcm16) < 4:
            return False
        arr = np.frombuffer(frame_pcm16, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(arr * arr)))
        return rms >= self._threshold


class MicCapture:
    """Blocking record-until-silence using sounddevice."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def mic_available(self) -> dict:
        try:
            import sounddevice as sd

            dev = sd.query_devices(kind="input")
            return {
                "ok": True,
                "device": str(dev.get("name", "default")),
                "sample_rate": SAMPLE_RATE,
            }
        except Exception as e:
            return {"ok": False, "device": "NOT_AVAILABLE", "error": str(e)}

    def record_until_silence(
        self,
        *,
        max_seconds: float = 12.0,
        silence_ms: int = 1400,
        pre_roll_ms: int = 300,
        vad_aggressiveness: int = 1,
        min_utterance_ms: int = 2800,
        pre_speech_timeout_ms: int = 6000,
        on_frame: Callable[[bytes], None] | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> bytes:
        """
        Capture one utterance.

        States:
          WAITING_FOR_SPEECH — ring-buffer only; initial silence is NOT end-of-speech.
          RECORDING — after first VAD speech; post-speech silence ends capture.
        """
        import sounddevice as sd

        pre_speech_ms = max(2000, int(pre_speech_timeout_ms or 6000))
        vdiag(
            "MIC_CAPTURE_START",
            max_seconds=max_seconds,
            silence_ms=silence_ms,
            pre_roll_ms=pre_roll_ms,
            vad_aggressiveness=vad_aggressiveness,
            min_utterance_ms=min_utterance_ms,
            pre_speech_timeout_ms=pre_speech_ms,
        )
        t_capture0 = time.perf_counter()
        vad = VoiceActivityDetector(aggressiveness=vad_aggressiveness)
        ring: collections.deque[bytes] = collections.deque(
            maxlen=max(1, pre_roll_ms // FRAME_MS)
        )
        voiced: list[bytes] = []
        silence_frames = 0
        silence_limit = max(1, silence_ms // FRAME_MS)
        max_frames = int(max_seconds * 1000 / FRAME_MS)
        heard_speech = False
        stopped = threading.Event()
        vad_started = False
        vad_ended = False
        pre_speech_timeout = False
        frames_received = 0

        def callback(indata, frames, time_info, status):  # noqa: ARG001
            nonlocal silence_frames, heard_speech, vad_started, vad_ended, frames_received
            if stop_check and stop_check():
                stopped.set()
                raise sd.CallbackStop()
            pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
            frames_received += 1
            if on_frame:
                on_frame(pcm)
            speech = vad.is_speech(pcm)
            if not heard_speech:
                # WAITING_FOR_SPEECH — do not treat silence as end-of-utterance.
                ring.append(pcm)
                if speech:
                    heard_speech = True
                    vad_started = True
                    vdiag("VAD_START", frames_received=frames_received)
                    voiced.extend(ring)
                    ring.clear()
                    silence_frames = 0
                return
            voiced.append(pcm)
            if speech:
                silence_frames = 0
            else:
                silence_frames += 1
                speech_ms = pcm_duration_ms(b"".join(voiced))
                # Tolerate brief pauses mid-phrase ("Hey Ira, ... open expenses").
                effective_limit = silence_limit
                if speech_ms < float(min_utterance_ms):
                    effective_limit = max(silence_limit, int(1800 / FRAME_MS))
                if silence_frames >= effective_limit:
                    vad_ended = True
                    vdiag("VAD_END", speech_ms=round(speech_ms, 1), silence_frames=silence_frames)
                    stopped.set()
                    raise sd.CallbackStop()

        frames_read = 0
        with self._lock:
            try:
                with sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=1,
                    dtype="float32",
                    blocksize=FRAME_SAMPLES,
                    callback=callback,
                ):
                    t0 = time.time()
                    while not stopped.is_set():
                        if stop_check and stop_check():
                            break
                        elapsed_ms = (time.time() - t0) * 1000.0
                        if not heard_speech and elapsed_ms >= pre_speech_ms:
                            pre_speech_timeout = True
                            vdiag(
                                "PRE_SPEECH_TIMEOUT",
                                elapsed_ms=round(elapsed_ms, 1),
                                frames_received=frames_received,
                            )
                            break
                        if time.time() - t0 > max_seconds:
                            break
                        time.sleep(FRAME_MS / 1000.0)
                        frames_read += 1
                        if frames_read > max_frames:
                            break
            except Exception as e:
                if "CallbackStop" not in type(e).__name__:
                    vlog("record_error", error=str(e))

        pcm = b"".join(voiced)
        capture_ms = (time.perf_counter() - t_capture0) * 1000.0
        vdiag(
            "MIC_CAPTURE_END",
            capture_ms=round(capture_ms, 1),
            audio_duration_ms=round(pcm_duration_ms(pcm), 1),
            audio_sample_count=len(pcm) // BYTES_PER_SAMPLE,
            pcm_bytes=len(pcm),
            vad_started=vad_started,
            vad_ended=vad_ended,
            heard_speech=heard_speech,
            pre_speech_timeout=pre_speech_timeout,
            frames_received=frames_received,
        )
        return pcm

    def stream_frames(
        self,
        *,
        frame_callback: Callable[[bytes], None],
        stop_check: Callable[[], bool],
    ) -> None:
        import sounddevice as sd

        frames_received = 0
        last_heartbeat = time.time()
        vdiag("MIC_STREAM_START")

        def callback(indata, frames, time_info, status):  # noqa: ARG001
            nonlocal frames_received, last_heartbeat
            if stop_check():
                raise sd.CallbackStop()
            pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
            frames_received += 1
            now = time.time()
            if now - last_heartbeat >= 5.0:
                vdiag("MIC_STREAM_FRAMES", frames_received=frames_received)
                last_heartbeat = now
            frame_callback(pcm)

        with self._lock:
            try:
                with sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=1,
                    dtype="float32",
                    blocksize=FRAME_SAMPLES,
                    callback=callback,
                ):
                    while not stop_check():
                        time.sleep(FRAME_MS / 1000.0)
            finally:
                vdiag("MIC_STREAM_END", frames_received=frames_received)
