"""Resolve bundled voice model paths (dev tree + PyInstaller frozen)."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _repo_voice_root() -> Path:
    return Path(__file__).resolve().parents[1] / "voice"


def _persistent_voice_models_dir() -> Path:
    """Writable model cache — ctranslate2 must not read Whisper weights from _MEIPASS."""
    try:
        from backend.runtime_paths import appdata_dir

        p = appdata_dir() / "voice" / "models"
    except Exception:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        p = base / "AICA" / "voice" / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _bundle_voice_models_dir() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    candidates: list[Path] = []
    if meipass:
        candidates.append(Path(meipass) / "desktop" / "voice" / "models")
    candidates.append(Path(sys.executable).resolve().parent / "voice" / "models")
    for c in candidates:
        if c.is_dir():
            return c
    return None


def _materialize_tree(src: Path, dest: Path) -> Path:
    """Copy bundled model tree to persistent storage if missing or stale."""
    marker = dest / ".aica_materialized"
    src_bin = src / "model.bin"
    dest_bin = dest / "model.bin"
    if (
        dest_bin.is_file()
        and src_bin.is_file()
        and dest_bin.stat().st_size == src_bin.stat().st_size
        and marker.is_file()
    ):
        return dest
    if dest.is_dir():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest)
    marker.write_text(str(src), encoding="utf-8")
    return dest


def voice_models_dir() -> Path:
    """Directory containing whisper + wake ONNX models."""
    override = os.environ.get("AICA_VOICE_MODELS_DIR")
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return p

    if getattr(sys, "frozen", False):
        persistent = _persistent_voice_models_dir()
        bundle = _bundle_voice_models_dir()
        if bundle is not None:
            # Materialize each subfolder (whisper, openwakeword, npz/json)
            for sub in bundle.iterdir():
                if sub.is_dir():
                    _materialize_tree(sub, persistent / sub.name)
                elif sub.is_file():
                    dest_file = persistent / sub.name
                    if not dest_file.is_file() or dest_file.stat().st_size != sub.stat().st_size:
                        shutil.copy2(sub, dest_file)
            return persistent
        return persistent

    p = _repo_voice_root() / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def whisper_model_dir() -> Path:
    explicit = os.environ.get("AICA_WHISPER_MODEL_DIR")
    if explicit:
        return Path(explicit)
    for name in ("whisper-small.en", "small.en"):
        p = voice_models_dir() / name
        if (p / "model.bin").is_file() or (p / "config.json").is_file():
            return p
    return voice_models_dir() / "whisper-small.en"


def wake_model_path() -> Path:
    explicit = os.environ.get("AICA_WAKE_MODEL")
    if explicit:
        return Path(explicit)
    return voice_models_dir() / "hey_ira.onnx"


def oww_resource_dir() -> Path:
    p = voice_models_dir() / "openwakeword"
    p.mkdir(parents=True, exist_ok=True)
    return p


def faster_whisper_assets_dir() -> Path:
    """Silero VAD ONNX used when vad_filter=True inside faster-whisper."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            p = Path(meipass) / "faster_whisper" / "assets"
            if p.is_dir():
                return p
    try:
        import faster_whisper

        return Path(faster_whisper.__file__).resolve().parent / "assets"
    except Exception:
        return Path(__file__).resolve().parents[1] / "voice" / "assets"
