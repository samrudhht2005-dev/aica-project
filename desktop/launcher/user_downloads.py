"""
Secure user file saves for the packaged AICA desktop WebView.

Browser blob/<a download> often fails silently in WebView2; this bridge writes
via Save As (preferred) or the Windows Downloads folder.
"""
from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path
from typing import Any

_MAX_BYTES = 40 * 1024 * 1024  # 40 MiB — invoices/labels stay well under this
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\- ()\[\]]+")
_ALLOWED_EXT = {".pdf", ".csv", ".json", ".png", ".txt"}


def sanitize_download_filename(name: str, default: str = "aica_download.pdf") -> str:
    raw = (name or "").strip().replace("\\", "_").replace("/", "_")
    raw = raw.split("\x00", 1)[0]
    raw = _SAFE_NAME.sub("_", raw).strip(" ._")
    if not raw or raw in {".", ".."}:
        raw = default
    if len(raw) > 180:
        stem, dot, ext = raw.rpartition(".")
        if dot and 1 <= len(ext) <= 8:
            raw = (stem[:160] or "aica_download") + "." + ext
        else:
            raw = raw[:180]
    stem, dot, ext = raw.rpartition(".")
    if not dot or f".{ext.lower()}" not in _ALLOWED_EXT:
        # Drop double extensions like invoice.pdf.exe
        base = stem if dot else raw
        base = base.split(".")[0] if base else "aica_download"
        raw = f"{base or 'aica_download'}.pdf"
    else:
        raw = f"{stem}.{ext.lower()}"
    return raw


def default_downloads_dir() -> Path:
    home = Path.home()
    for candidate in (
        Path(os.environ.get("USERPROFILE", "")) / "Downloads",
        home / "Downloads",
        home / "Desktop",
        home,
    ):
        try:
            if candidate and str(candidate) not in {".", ""}:
                candidate.mkdir(parents=True, exist_ok=True)
                return candidate
        except Exception:
            continue
    return Path.cwd()


def unique_path(directory: Path, filename: str) -> Path:
    target = directory / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for i in range(2, 500):
        candidate = directory / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}_{os.getpid()}{suffix}"


def decode_download_payload(content_b64: str) -> bytes:
    if not isinstance(content_b64, str) or not content_b64.strip():
        raise ValueError("Empty download payload")
    # Strip data-URL prefix if a caller passes one by mistake
    payload = content_b64.strip()
    if "," in payload and payload.lower().startswith("data:"):
        payload = payload.split(",", 1)[1]
    raw = base64.b64decode(payload, validate=False)
    if len(raw) > _MAX_BYTES:
        raise ValueError("File too large to save")
    if not raw:
        raise ValueError("Empty file")
    return raw


def save_bytes_with_dialog(
    window,
    filename: str,
    data: bytes,
) -> dict[str, Any]:
    """
    Prefer native Save As via pywebview; fall back to Downloads.
    Never trusts caller-supplied filesystem paths — only a basename.
    """
    safe = sanitize_download_filename(filename)
    downloads = default_downloads_dir()

    chosen: Path | None = None
    cancelled = False
    if window is not None:
        try:
            import webview

            result = window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(downloads),
                save_filename=safe,
                file_types=("PDF (*.pdf)", "All files (*.*)"),
            )
            if not result:
                cancelled = True
            else:
                pick = result[0] if isinstance(result, (list, tuple)) else result
                # Only accept a file path under a user-chosen location; still use basename safety
                chosen = Path(str(pick))
                # Force safe name if dialog returned a weird path
                if chosen.name != sanitize_download_filename(chosen.name, default=safe):
                    chosen = chosen.with_name(sanitize_download_filename(chosen.name, default=safe))
        except Exception as e:
            logging.warning("AICA Save As dialog failed, using Downloads: %s", e)
            chosen = None

    if cancelled:
        return {"ok": False, "cancelled": True, "error": "Save cancelled"}

    if chosen is None:
        chosen = unique_path(downloads, safe)

    try:
        chosen.parent.mkdir(parents=True, exist_ok=True)
        chosen.write_bytes(data)
    except Exception as e:
        logging.exception("AICA download save failed")
        return {"ok": False, "cancelled": False, "error": f"Could not save file: {e}"}

    return {
        "ok": True,
        "cancelled": False,
        "filename": chosen.name,
        "folder": str(chosen.parent),
        "path": str(chosen),
        "bytes": len(data),
    }
