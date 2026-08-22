"""Resolve bundled voice model paths (dev tree + PyInstaller frozen)."""
from __future__ import annotations

import os
import shutil
import sys
import time
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


def _tree_fingerprint(root: Path) -> tuple[tuple[str, int], ...]:
    """Stable (relative_path, size) pairs for materialization freshness checks."""
    rows: list[tuple[str, int]] = []
    if not root.is_dir():
        return tuple(rows)
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.name == ".aica_materialized":
            continue
        rel = p.relative_to(root).as_posix()
        try:
            rows.append((rel, int(p.stat().st_size)))
        except OSError:
            continue
    return tuple(rows)


def _materialize_tree(src: Path, dest: Path) -> Path:
    """
    Copy bundled model tree to persistent storage if missing or stale.

    Idempotent under concurrent warm-up: if another process already materialized
    a matching tree, return dest without raising WinError 183.
    """
    marker = dest / ".aica_materialized"
    src_fp = _tree_fingerprint(src)
    if not src_fp:
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    if dest.is_dir() and marker.is_file():
        try:
            if _tree_fingerprint(dest) == src_fp:
                return dest
        except OSError:
            pass

    lock = dest.parent / f".{dest.name}.materialize.lock"
    dest.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + 30.0
    while time.time() < deadline:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("ascii", "ignore"))
            finally:
                os.close(fd)
            break
        except FileExistsError:
            # Another materialize in progress — wait, then re-check freshness.
            time.sleep(0.15)
            if dest.is_dir() and marker.is_file():
                try:
                    if _tree_fingerprint(dest) == src_fp:
                        return dest
                except OSError:
                    pass
            continue
    else:
        # Timed out waiting for lock; use existing dest if usable.
        if dest.is_dir() and any(dest.iterdir()):
            return dest
        raise TimeoutError(f"Timed out materializing voice models to {dest}")

    try:
        # Re-check under lock (another waiter may have finished).
        if dest.is_dir() and marker.is_file():
            try:
                if _tree_fingerprint(dest) == src_fp:
                    return dest
            except OSError:
                pass

        staging = dest.parent / f".{dest.name}.staging.{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(src, staging)
        (staging / ".aica_materialized").write_text(str(src), encoding="utf-8")

        # Atomic-ish replace: remove old dest then rename staging → dest.
        if dest.exists():
            backup = dest.parent / f".{dest.name}.old.{os.getpid()}"
            try:
                if backup.exists():
                    shutil.rmtree(backup, ignore_errors=True)
                os.replace(str(dest), str(backup))
                shutil.rmtree(backup, ignore_errors=True)
            except OSError:
                shutil.rmtree(dest, ignore_errors=True)

        try:
            os.replace(str(staging), str(dest))
        except OSError as e:
            # WinError 183 / destination exists — another process won the race.
            shutil.rmtree(staging, ignore_errors=True)
            if dest.is_dir() and any(dest.iterdir()):
                return dest
            raise e
        return dest
    finally:
        try:
            lock.unlink(missing_ok=True)
        except TypeError:
            # Python <3.8 compatibility (not expected here)
            if lock.exists():
                try:
                    lock.unlink()
                except OSError:
                    pass
        except OSError:
            pass


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
                        try:
                            shutil.copy2(sub, dest_file)
                        except OSError:
                            if dest_file.is_file():
                                pass
                            else:
                                raise
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
    # Do not mkdir here if materialization owns this path — only ensure parent exists.
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
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
