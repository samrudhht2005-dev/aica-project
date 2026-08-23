"""Secure installer download for AICA auto-update (Phase 4 — download/verify only)."""
from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from desktop.launcher.update_checker import compare_versions, get_update_state
from desktop.launcher.update_config import (
    SHA256_HEX_RE,
    _validate_https_url,
    expected_installer_filename,
    load_update_config,
)
from desktop.launcher.update_log import ulog

DOWNLOAD_CHUNK_BYTES = 1024 * 1024
DOWNLOAD_CONNECT_TIMEOUT_S = 30.0
DOWNLOAD_READ_TIMEOUT_S = 120.0
PROGRESS_MIN_INTERVAL_S = 0.4
DISK_SPACE_MULTIPLIER = 2.2

_download_lock = threading.RLock()
_download_thread: threading.Thread | None = None
_download_state: DownloadState | None = None


@dataclass
class DownloadState:
    status: str  # idle, starting, downloading, verifying, ready, error
    version: str | None = None
    bytes_downloaded: int = 0
    total_bytes: int = 0
    progress_percent: float | None = None
    sha_verified: bool = False
    error: str | None = None
    user_message: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "version": self.version,
            "bytes_downloaded": int(self.bytes_downloaded),
            "total_bytes": int(self.total_bytes),
            "progress_percent": self.progress_percent,
            "sha_verified": bool(self.sha_verified),
            "error": self.user_message or self.error,
            "active": self.status in ("starting", "downloading", "verifying"),
        }


def _set_download_state(state: DownloadState) -> None:
    global _download_state
    with _download_lock:
        _download_state = state


def _get_download_state() -> DownloadState:
    with _download_lock:
        if _download_state is None:
            return DownloadState(status="idle")
        return _download_state


def _is_download_active() -> bool:
    state = _get_download_state()
    return state.status in ("starting", "downloading", "verifying")


def staging_dir(version: str) -> Path:
    base = os.environ.get("TEMP") or tempfile.gettempdir()
    path = Path(base) / "AICA" / "updates" / version
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sha256_file(path: Path, *, chunk_size: int = DOWNLOAD_CHUNK_BYTES) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _file_matches(path: Path, *, size_bytes: int, sha256: str) -> bool:
    if not path.is_file():
        return False
    try:
        actual_size = path.stat().st_size
    except OSError:
        return False
    if actual_size != size_bytes:
        return False
    try:
        actual_sha = _sha256_file(path)
    except OSError:
        return False
    return hmac.compare_digest(actual_sha.lower(), sha256.lower())


def _safe_remove(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def _validate_installer_metadata(installer: dict[str, Any], version: str) -> str | None:
    url = str(installer.get("url") or "").strip()
    err = _validate_https_url(url, field="installer.url")
    if err:
        return err

    filename = str(installer.get("filename") or "").strip()
    expected = expected_installer_filename(version)
    if filename != expected:
        return f"unexpected installer filename: {filename!r}"

    sha256 = str(installer.get("sha256") or "").strip().lower()
    if not SHA256_HEX_RE.match(sha256):
        return "invalid installer sha256"

    size_bytes = installer.get("size_bytes")
    if not isinstance(size_bytes, int) or size_bytes <= 0:
        return "invalid installer size_bytes"

    return None


def _authoritative_installer() -> tuple[str, dict[str, Any], str] | tuple[None, None, str]:
    config, cfg_err = load_update_config()
    if config is None:
        return None, None, cfg_err or "invalid update configuration"

    state = get_update_state()
    if state is None or not state.update_available:
        return None, None, "no update available"

    if state.status != "update_available":
        return None, None, "update is no longer available"

    version = str(state.available_version or "").strip()
    if not version:
        return None, None, "missing available version"

    cmp = compare_versions(version, config.installed_version)
    if cmp is None or cmp <= 0:
        return None, None, "update version is not newer than installed version"

    installer = state.installer
    if not isinstance(installer, dict):
        return None, None, "missing installer metadata"

    meta_err = _validate_installer_metadata(installer, version)
    if meta_err:
        return None, None, meta_err

    return version, installer, ""


def _disk_space_error(staging: Path, required_bytes: int) -> str | None:
    try:
        usage = shutil.disk_usage(staging)
    except OSError as e:
        return f"disk check failed: {e}"
    need = int(required_bytes * DISK_SPACE_MULTIPLIER)
    if usage.free < need:
        ulog("update_download_insufficient_storage", free=usage.free, need=need)
        return "insufficient_storage"
    return None


def _mark_ready(version: str, final_path: Path) -> None:
    _set_download_state(
        DownloadState(
            status="ready",
            version=version,
            bytes_downloaded=final_path.stat().st_size,
            total_bytes=final_path.stat().st_size,
            progress_percent=100.0,
            sha_verified=True,
        )
    )
    ulog("update_download_ready", version=version, size=final_path.stat().st_size)


def _mark_error(version: str | None, *, code: str, user_message: str, log_extra: dict | None = None) -> None:
    extra = log_extra or {}
    ulog("update_download_failed", version=version or "", code=code, **extra)
    _set_download_state(
        DownloadState(
            status="error",
            version=version,
            error=code,
            user_message=user_message,
        )
    )


def _try_reuse_existing(version: str, final_path: Path, *, size_bytes: int, sha256: str) -> bool:
    if not final_path.is_file():
        return False
    if _file_matches(final_path, size_bytes=size_bytes, sha256=sha256):
        ulog("update_download_reuse_verified", version=version)
        _mark_ready(version, final_path)
        return True
    ulog("update_download_reuse_invalid", version=version)
    _safe_remove(final_path)
    _safe_remove(final_path.with_suffix(final_path.suffix + ".part"))
    return False


def _download_to_part(
    url: str,
    part_path: Path,
    *,
    total_bytes: int,
    version: str,
    user_agent: str,
) -> str | None:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "*/*"},
        method="GET",
    )
    last_progress_at = 0.0
    downloaded = 0

    try:
        # urllib.urlopen accepts a single timeout (seconds) applied to blocking
        # socket ops — not a (connect, read) tuple (that raises TypeError).
        with urllib.request.urlopen(
            req,
            timeout=max(DOWNLOAD_CONNECT_TIMEOUT_S, DOWNLOAD_READ_TIMEOUT_S),
        ) as resp:
            content_length = resp.headers.get("Content-Length")
            if content_length is not None:
                try:
                    header_total = int(content_length)
                    if header_total > 0:
                        total_bytes = header_total
                except ValueError:
                    pass

            _set_download_state(
                DownloadState(
                    status="downloading",
                    version=version,
                    bytes_downloaded=0,
                    total_bytes=total_bytes,
                    progress_percent=0.0 if total_bytes > 0 else None,
                )
            )

            with open(part_path, "wb") as out:
                while True:
                    chunk = resp.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    if now - last_progress_at >= PROGRESS_MIN_INTERVAL_S:
                        pct = (
                            round((downloaded / total_bytes) * 100.0, 1)
                            if total_bytes > 0
                            else None
                        )
                        _set_download_state(
                            DownloadState(
                                status="downloading",
                                version=version,
                                bytes_downloaded=downloaded,
                                total_bytes=total_bytes,
                                progress_percent=pct,
                            )
                        )
                        last_progress_at = now
                out.flush()
                os.fsync(out.fileno())

        pct = round((downloaded / total_bytes) * 100.0, 1) if total_bytes > 0 else None
        _set_download_state(
            DownloadState(
                status="downloading",
                version=version,
                bytes_downloaded=downloaded,
                total_bytes=total_bytes,
                progress_percent=pct,
            )
        )
        ulog("update_download_completed", version=version, bytes=downloaded)
        return None
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.timeout):
            return "timeout"
        return "connection_failed"
    except socket.timeout:
        return "timeout"
    except OSError as e:
        return str(e)


def _verify_and_finalize(
    version: str,
    part_path: Path,
    final_path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> str | None:
    _set_download_state(
        DownloadState(
            status="verifying",
            version=version,
            bytes_downloaded=part_path.stat().st_size if part_path.is_file() else 0,
            total_bytes=expected_size,
            progress_percent=None,
        )
    )

    try:
        actual_size = part_path.stat().st_size
    except OSError:
        return "size_validation_failed"

    if actual_size != expected_size:
        ulog(
            "update_download_size_mismatch",
            version=version,
            expected=expected_size,
            actual=actual_size,
        )
        return "size_mismatch"

    try:
        actual_sha = _sha256_file(part_path)
    except OSError:
        return "sha_validation_failed"

    if not hmac.compare_digest(actual_sha.lower(), expected_sha256.lower()):
        ulog("update_download_sha_mismatch", version=version)
        return "sha_mismatch"

    try:
        os.replace(part_path, final_path)
    except OSError as e:
        ulog("update_download_rename_failed", version=version, error=str(e))
        return "finalize_failed"

    ulog("update_download_sha_ok", version=version)
    return None


def _user_message_for_code(code: str) -> str:
    mapping = {
        "insufficient_storage": "Not enough disk space to download the update.",
        "timeout": "The connection timed out while downloading the update.",
        "connection_failed": "The download was interrupted. Check your connection and try again.",
        "size_mismatch": "The downloaded file is invalid.",
        "sha_mismatch": "Unable to verify the downloaded update.",
        "sha_validation_failed": "Unable to verify the downloaded update.",
        "size_validation_failed": "The downloaded file is invalid.",
        "finalize_failed": "Unable to prepare the downloaded update.",
        "no update available": "The update is no longer available.",
        "update is no longer available": "The update is no longer available.",
    }
    if code.startswith("HTTP "):
        return "Download failed. Please try again later."
    return mapping.get(code, "Download failed. Please try again later.")


def _download_worker() -> None:
    global _download_thread
    version: str | None = None
    part_path: Path | None = None
    try:
        version, installer, err = _authoritative_installer()
        if version is None or installer is None:
            _mark_error(None, code=err or "invalid_update", user_message=_user_message_for_code(err or ""))
            return

        url = str(installer["url"])
        size_bytes = int(installer["size_bytes"])
        sha256 = str(installer["sha256"]).lower()
        staging = staging_dir(version)
        final_name = expected_installer_filename(version)
        final_path = staging / final_name
        part_path = final_path.parent / (final_name + ".part")

        _set_download_state(
            DownloadState(status="starting", version=version, total_bytes=size_bytes, progress_percent=0.0)
        )
        ulog("update_download_started", version=version, size=size_bytes)

        if _try_reuse_existing(version, final_path, size_bytes=size_bytes, sha256=sha256):
            return

        disk_err = _disk_space_error(staging, size_bytes)
        if disk_err:
            _mark_error(version, code=disk_err, user_message=_user_message_for_code(disk_err))
            return

        _safe_remove(part_path)

        config, _ = load_update_config()
        installed = config.installed_version if config else version
        user_agent = f"AICA-Updater/{installed}"

        fetch_err = _download_to_part(
            url,
            part_path,
            total_bytes=size_bytes,
            version=version,
            user_agent=user_agent,
        )
        if fetch_err:
            _safe_remove(part_path)
            _mark_error(
                version,
                code=fetch_err,
                user_message=_user_message_for_code(fetch_err),
            )
            return

        verify_err = _verify_and_finalize(
            version,
            part_path,
            final_path,
            expected_size=size_bytes,
            expected_sha256=sha256,
        )
        if verify_err:
            _safe_remove(part_path)
            _safe_remove(final_path)
            _mark_error(
                version,
                code=verify_err,
                user_message=_user_message_for_code(verify_err),
            )
            return

        _mark_ready(version, final_path)
    except Exception as e:
        ulog("update_download_unhandled", version=version or "", error=str(e))
        if part_path:
            _safe_remove(part_path)
        _mark_error(version, code="error", user_message=_user_message_for_code("error"))
    finally:
        with _download_lock:
            _download_thread = None


def get_update_download_status_dict() -> dict[str, Any]:
    """JSON-safe download progress for pywebview (no filesystem paths)."""
    return _get_download_state().to_public_dict()


def get_ready_installer_info() -> tuple[dict[str, Any] | None, str | None]:
    """
    Internal Phase 5 helper: authoritative path/hash for a verified ready installer.
    Not exposed to the frontend.
    """
    state = _get_download_state()
    if state.status != "ready" or not state.sha_verified or not state.version:
        return None, "no verified installer ready"

    version, installer, err = _authoritative_installer()
    if version is None or installer is None:
        # Fall back to ready-state version + staged file if update state raced away,
        # but still require on-disk re-verification of size/hash from last known metadata.
        return None, err or "update metadata unavailable"

    if version != state.version:
        return None, "ready version mismatch"

    sha256 = str(installer["sha256"]).lower()
    size_bytes = int(installer["size_bytes"])
    staging = staging_dir(version)
    final_name = expected_installer_filename(version)
    final_path = staging / final_name

    if not _file_matches(final_path, size_bytes=size_bytes, sha256=sha256):
        return None, "staged installer failed re-verification"

    return {
        "version": version,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "filename": final_name,
        "staging_dir": str(staging),
        "installer_path": str(final_path),
    }, None


def start_update_download() -> dict[str, Any]:
    """
    Begin background installer download using validated update state only.
    Returns immediately; poll get_update_download_status_dict() for progress.
    """
    global _download_thread

    with _download_lock:
        if _download_thread is not None and _download_thread.is_alive():
            payload = _get_download_state().to_public_dict()
            payload["ok"] = False
            payload["already_in_progress"] = True
            return payload

        state = _get_download_state()
        if state.status == "ready" and state.version:
            version, installer, err = _authoritative_installer()
            if version and installer and not err:
                staging = staging_dir(version)
                final_path = staging / expected_installer_filename(version)
                if _file_matches(
                    final_path,
                    size_bytes=int(installer["size_bytes"]),
                    sha256=str(installer["sha256"]),
                ):
                    payload = state.to_public_dict()
                    payload["ok"] = True
                    payload["already_ready"] = True
                    return payload

        if _is_download_active():
            payload = _get_download_state().to_public_dict()
            payload["ok"] = False
            payload["already_in_progress"] = True
            return payload

        version, installer, err = _authoritative_installer()
        if version is None or installer is None:
            return {
                "ok": False,
                "already_in_progress": False,
                "status": "error",
                "error": _user_message_for_code(err or "invalid_update"),
            }

        _set_download_state(DownloadState(status="starting", version=version))
        _download_thread = threading.Thread(
            target=_download_worker,
            name="aica-update-download",
            daemon=True,
        )
        _download_thread.start()

    payload = _get_download_state().to_public_dict()
    payload["ok"] = True
    payload["already_in_progress"] = False
    return payload
