"""Shared wake PCM preprocessing — VAD clip minimum-length safety for OWW embedding."""
from __future__ import annotations

import numpy as np

from desktop.launcher.voice_audio import FRAME_SAMPLES, SAMPLE_RATE, VoiceActivityDetector

# OWW mel+embedding needs ~800 ms (76-frame window @ 8-frame hop).
MIN_EMBED_SAMPLES = int(0.80 * SAMPLE_RATE)
MIN_EMBED_DURATION_S = MIN_EMBED_SAMPLES / SAMPLE_RATE


def pcm_duration_s(pcm: bytes) -> float:
    return len(pcm) / (SAMPLE_RATE * 2) if pcm else 0.0


def pad_pcm_center(pcm: bytes, min_samples: int = MIN_EMBED_SAMPLES) -> bytes:
    arr = np.frombuffer(pcm, dtype=np.int16)
    if arr.size >= min_samples:
        return pcm
    out = np.zeros(min_samples, dtype=np.int16)
    start = (min_samples - arr.size) // 2
    out[start : start + arr.size] = arr
    return out.tobytes()


def can_embed_pcm(pcm: bytes, embedder=None) -> bool:
    if not pcm or len(pcm) < MIN_EMBED_SAMPLES * 2:
        return False
    from desktop.launcher.voice_wake_util import OwwOnnxEmbedder

    emb = embedder or OwwOnnxEmbedder()
    audio = np.frombuffer(pcm, dtype=np.int16)
    try:
        emb.embed_audio(audio)
        return True
    except ValueError:
        return False


def ensure_embeddable_pcm(pcm: bytes) -> tuple[bytes, str]:
    """
    Ensure PCM meets OWW embedding minimum length.

    Production wake buffers are VAD-trimmed speech segments that can be ~0.58–0.72 s.
    Center-pad short clips rather than silently failing embedding.
    """
    if not pcm:
        return pcm, "empty"
    if can_embed_pcm(pcm):
        return pcm, "as_is"
    padded = pad_pcm_center(pcm, MIN_EMBED_SAMPLES)
    if can_embed_pcm(padded):
        return padded, "padded_center"
    return pcm, "unembeddable"


def extract_vad_speech_segment(pcm: bytes) -> tuple[bytes, bool]:
    """
    VAD speech segment from PCM (matches EmbeddingWakeDetector buffer contents).
    For fixed recordings, simulates what the live wake buffer would contain.
    """
    if not pcm:
        return pcm, False
    vad = VoiceActivityDetector(aggressiveness=2)
    frame_bytes = FRAME_SAMPLES * 2
    min_speech_s = 0.28
    max_speech_s = 2.2
    silence_limit = 10

    buf = bytearray()
    in_speech = False
    speech_started = 0.0
    silence_frames = 0
    best = b""
    found_speech = False

    t = 0.0
    for i in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
        frame = pcm[i : i + frame_bytes]
        t = i / (SAMPLE_RATE * 2)
        if vad.is_speech(frame):
            found_speech = True
            if not in_speech:
                in_speech = True
                speech_started = t
                buf.clear()
                silence_frames = 0
            buf.extend(frame)
            silence_frames = 0
            if t - speech_started >= max_speech_s:
                best = bytes(buf)
                break
        elif in_speech:
            buf.extend(frame)
            silence_frames += 1
            if silence_frames >= silence_limit:
                if t - speech_started >= min_speech_s:
                    best = bytes(buf)
                break

    if not best and len(buf) >= int(min_speech_s * SAMPLE_RATE) * 2:
        best = bytes(buf)
    if best:
        return best, found_speech
    return pcm, found_speech


def prepare_production_wake_pcm(pcm: bytes) -> tuple[bytes, dict]:
    """
    Production-equivalent wake clip for embedding from either:
    - live VAD buffer (already trimmed), or
    - fixed calibration recording (VAD extract first).
    """
    meta = {
        "input_duration_s": round(pcm_duration_s(pcm), 3),
        "vad_extracted": False,
        "vad_duration_s": None,
        "preprocess": "unknown",
        "scored_duration_s": None,
        "embed_ok": False,
    }
    # Heuristic: fixed recordings > 2s are calibration captures — extract VAD first.
    if pcm_duration_s(pcm) > 2.0:
        vad_pcm, found = extract_vad_speech_segment(pcm)
        meta["vad_extracted"] = True
        meta["vad_duration_s"] = round(pcm_duration_s(vad_pcm), 3)
        work = vad_pcm if found else pcm
    else:
        work = pcm
        meta["vad_duration_s"] = meta["input_duration_s"]

    scored, how = ensure_embeddable_pcm(work)
    meta["preprocess"] = how
    meta["scored_duration_s"] = round(pcm_duration_s(scored), 3)
    meta["embed_ok"] = how != "unembeddable"
    return scored, meta
