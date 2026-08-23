"""Background update manifest check (Phase 2 — no download, no UI)."""
from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

from backend.runtime_paths import appdata_dir

from desktop.launcher.update_config import (
    ALLOWED_UPDATE_HOSTS,
    INSTALLER_FILENAME_RE,
    SEMVER_RELEASE_RE,
    SHA256_HEX_RE,
    VALID_CHANNELS,
    UpdateConfig,
    expected_installer_filename,
    load_update_config,
)
from desktop.launcher.update_log import ulog

MANIFEST_FETCH_TIMEOUT_S = 5.0
CACHE_TTL_S = 24 * 60 * 60
CACHE_FILENAME = "update_check.json"

# Supported: numeric major.minor.patch release versions (1.0.10 > 1.0.9).
# Pre-release suffixes in manifest are accepted by regex but compared on numeric triple only.

_state_lock = threading.Lock()
_latest_state: UpdateState | None = None
_check_in_progress = False


@dataclass
class UpdateState:
    status: str
    installed_version: str
    update_available: bool = False
    available_version: str | None = None
    mandatory: bool = False
    release_notes: str = ""
    published_at: str | None = None
    installer: dict[str, Any] | None = None
    last_check_at: str | None = None
    from_cache: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_release_triple(version: str) -> tuple[int, int, int] | None:
    """Parse major.minor.patch; ignores pre-release/build suffix for ordering."""
    version = (version or "").strip()
    m = re.match(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def compare_versions(left: str, right: str) -> int | None:
    """
    Compare release triples numerically.
    Returns -1 if left < right, 0 if equal, 1 if left > right, None if invalid.
    """
    a = parse_release_triple(left)
    b = parse_release_triple(right)
    if a is None or b is None:
        return None
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


def _cache_path():
    return appdata_dir() / CACHE_FILENAME


def _parse_iso_timestamp(ts: str) -> float | None:
    if not ts:
        return None
    try:
        # Accept Z or numeric offset
        normalized = ts.replace("Z", "+00:00")
        if re.search(r"\d{2}:\d{2}:\d{2}\.\d+", normalized):
            dt = datetime.fromisoformat(normalized)
        else:
            dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def read_update_cache() -> dict[str, Any] | None:
    path = _cache_path()
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        ulog("update_cache_corrupt", path=str(path))
        return None


def write_update_cache(payload: dict[str, Any]) -> None:
    try:
        path = _cache_path()
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as e:
        ulog("update_cache_write_failed", error=str(e))


def _cache_is_fresh(cache: dict[str, Any], installed_version: str) -> bool:
    if str(cache.get("installed_version") or "") != installed_version:
        return False
    ts = _parse_iso_timestamp(str(cache.get("last_check_at") or ""))
    if ts is None:
        return False
    return (time.time() - ts) < CACHE_TTL_S


def _state_from_cache(cache: dict[str, Any]) -> UpdateState | None:
    try:
        status = str(cache.get("status") or "")
        if not status:
            return None
        return UpdateState(
            status=status,
            installed_version=str(cache.get("installed_version") or ""),
            update_available=bool(cache.get("update_available")),
            available_version=cache.get("available_version"),
            mandatory=bool(cache.get("mandatory")),
            release_notes=str(cache.get("release_notes") or ""),
            installer=cache.get("manifest_summary") or cache.get("installer"),
            last_check_at=cache.get("last_check_at"),
            from_cache=True,
            error=cache.get("error"),
        )
    except Exception:
        return None


def _cache_payload_from_state(state: UpdateState) -> dict[str, Any]:
    return {
        "last_check_at": state.last_check_at or _utc_now_iso(),
        "installed_version": state.installed_version,
        "available_version": state.available_version,
        "update_available": state.update_available,
        "status": state.status,
        "mandatory": state.mandatory,
        "release_notes": state.release_notes,
        "manifest_summary": state.installer,
        "error": state.error,
    }


def _validate_published_at(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    return _parse_iso_timestamp(value.strip()) is not None


def validate_manifest(data: Any, *, installed_version: str) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(data, dict):
        return None, "manifest is not a JSON object"

    version = str(data.get("version") or "").strip()
    if not SEMVER_RELEASE_RE.match(version):
        return None, f"invalid manifest version: {version!r}"

    if not _validate_published_at(data.get("published_at")):
        return None, "invalid or missing published_at"

    channel = str(data.get("channel") or "stable").strip().lower()
    if channel not in VALID_CHANNELS:
        return None, f"invalid channel: {channel!r}"

    mandatory = data.get("mandatory", False)
    if not isinstance(mandatory, bool):
        return None, "mandatory must be boolean"

    release_notes = data.get("release_notes")
    if not isinstance(release_notes, str) or not release_notes.strip():
        return None, "release_notes missing or empty"

    min_supported = data.get("minimum_supported_version")
    if min_supported is not None:
        if not isinstance(min_supported, str) or not SEMVER_RELEASE_RE.match(min_supported.strip()):
            return None, "invalid minimum_supported_version"

    installer = data.get("installer")
    if not isinstance(installer, dict):
        return None, "installer section missing"

    filename = str(installer.get("filename") or "").strip()
    expected = expected_installer_filename(version)
    if filename != expected or not INSTALLER_FILENAME_RE.match(filename):
        return None, f"invalid installer filename: {filename!r}"

    url = str(installer.get("url") or "").strip()
    if not url.lower().startswith("https://"):
        return None, "installer URL must use HTTPS"
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None, "installer URL is invalid"
    if host not in ALLOWED_UPDATE_HOSTS:
        return None, f"installer URL host not allowlisted: {host}"

    sha256 = str(installer.get("sha256") or "").strip().lower()
    if not SHA256_HEX_RE.match(sha256):
        return None, "invalid installer sha256"

    size_bytes = installer.get("size_bytes")
    if not isinstance(size_bytes, int) or size_bytes <= 0:
        return None, "invalid installer size_bytes"

    cmp = compare_versions(version, installed_version)
    if cmp is None:
        return None, "version comparison failed"

    return {
        "version": version,
        "published_at": str(data.get("published_at")).strip(),
        "channel": channel,
        "minimum_supported_version": min_supported,
        "mandatory": mandatory,
        "release_notes": release_notes.strip(),
        "installer": {
            "filename": filename,
            "url": url,
            "sha256": sha256,
            "size_bytes": size_bytes,
        },
        "_compare": cmp,
    }, None


def _fetch_manifest(url: str, *, timeout: float, user_agent: str) -> tuple[dict[str, Any] | None, str | None]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            return None, "manifest JSON root is not an object"
        return data, None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.timeout):
            return None, "timeout"
        return None, "no_network"
    except socket.timeout:
        return None, "timeout"
    except json.JSONDecodeError:
        return None, "invalid JSON"
    except Exception as e:
        return None, str(e)


def _set_latest_state(state: UpdateState) -> None:
    global _latest_state
    with _state_lock:
        _latest_state = state


def get_update_state() -> UpdateState | None:
    with _state_lock:
        return _latest_state


def check_for_updates(*, force: bool = False, use_cache: bool = True) -> UpdateState:
    """
    Check GitHub release manifest and return structured update state.
    Synchronous — intended for tests and explicit refresh; startup uses background thread.
    """
    config, cfg_err = load_update_config()
    if config is None:
        state = UpdateState(
            status="invalid_config",
            installed_version="",
            error=cfg_err or "invalid update configuration",
            last_check_at=_utc_now_iso(),
        )
        _set_latest_state(state)
        ulog("update_check_invalid_config", error=state.error)
        return state

    installed = config.installed_version

    if use_cache and not force:
        cache = read_update_cache()
        if cache and _cache_is_fresh(cache, installed):
            cached_state = _state_from_cache(cache)
            if cached_state is not None:
                ulog(
                    "update_check_cache_used",
                    status=cached_state.status,
                    installed=installed,
                )
                _set_latest_state(cached_state)
                return cached_state

    ulog("update_check_started", installed=installed, force=force)
    user_agent = f"AICA-Updater/{installed}"

    raw, fetch_err = _fetch_manifest(
        config.manifest_url,
        timeout=MANIFEST_FETCH_TIMEOUT_S,
        user_agent=user_agent,
    )
    if raw is None:
        status = "timeout" if fetch_err == "timeout" else "no_network" if fetch_err == "no_network" else "error"
        state = UpdateState(
            status=status,
            installed_version=installed,
            error=fetch_err,
            last_check_at=_utc_now_iso(),
        )
        _set_latest_state(state)
        ulog("update_check_fetch_failed", status=status, error=fetch_err)
        return state

    ulog("update_manifest_fetched", version=raw.get("version"))

    validated, manifest_err = validate_manifest(raw, installed_version=installed)
    if validated is None:
        state = UpdateState(
            status="invalid_manifest",
            installed_version=installed,
            error=manifest_err,
            last_check_at=_utc_now_iso(),
        )
        _set_latest_state(state)
        ulog("update_manifest_rejected", error=manifest_err)
        return state

    manifest_version = validated["version"]
    cmp = validated["_compare"]

    if cmp > 0:
        if config.channel == "stable" and validated["channel"] != "stable":
            state = UpdateState(
                status="invalid_manifest",
                installed_version=installed,
                error=f"non-stable channel {validated['channel']!r} ignored on stable client",
                last_check_at=_utc_now_iso(),
            )
            _set_latest_state(state)
            ulog("update_manifest_rejected", error=state.error)
            return state

        inst = validated["installer"]
        state = UpdateState(
            status="update_available",
            installed_version=installed,
            update_available=True,
            available_version=manifest_version,
            mandatory=validated["mandatory"],
            release_notes=validated["release_notes"],
            published_at=validated.get("published_at"),
            installer=dict(inst),
            last_check_at=_utc_now_iso(),
        )
        write_update_cache(_cache_payload_from_state(state))
        _set_latest_state(state)
        ulog("update_available", installed=installed, available=manifest_version)
        return state

    if cmp == 0:
        state = UpdateState(
            status="up_to_date",
            installed_version=installed,
            update_available=False,
            available_version=manifest_version,
            published_at=validated.get("published_at"),
            last_check_at=_utc_now_iso(),
        )
        write_update_cache(_cache_payload_from_state(state))
        _set_latest_state(state)
        ulog("update_up_to_date", version=installed)
        return state

    state = UpdateState(
        status="older_manifest_ignored",
        installed_version=installed,
        update_available=False,
        available_version=manifest_version,
        last_check_at=_utc_now_iso(),
        error=f"manifest version {manifest_version} is older than installed {installed}",
    )
    write_update_cache(_cache_payload_from_state(state))
    _set_latest_state(state)
    ulog("update_older_manifest_ignored", installed=installed, manifest=manifest_version)
    return state


def _background_check(*, force: bool) -> None:
    global _check_in_progress
    try:
        check_for_updates(force=force, use_cache=not force)
    except Exception as e:
        ulog("update_check_unhandled_error", error=str(e))
        config, _ = load_update_config()
        installed = config.installed_version if config else ""
        _set_latest_state(
            UpdateState(
                status="error",
                installed_version=installed,
                error=str(e),
                last_check_at=_utc_now_iso(),
            )
        )
    finally:
        with _state_lock:
            _check_in_progress = False


def schedule_background_update_check(*, delay_s: float = 2.0, force: bool = False) -> None:
    """
    Schedule a non-blocking update check after UI load (daemon thread).
    Safe to call from WebView loaded callback — returns immediately.
    """
    global _check_in_progress

    with _state_lock:
        if _check_in_progress:
            ulog("update_check_skipped", reason="already_in_progress")
            return
        _check_in_progress = True

    def _run() -> None:
        global _check_in_progress
        try:
            if delay_s > 0:
                time.sleep(delay_s)
            _background_check(force=force)
        except Exception as e:
            ulog("update_check_thread_error", error=str(e))
            with _state_lock:
                _check_in_progress = False

    threading.Thread(target=_run, name="aica-update-check", daemon=True).start()


def is_update_check_in_progress() -> bool:
    with _state_lock:
        return _check_in_progress


def get_update_status_dict() -> dict[str, Any]:
    """
    JSON-safe update status for pywebview / frontend (no secrets or filesystem paths).
    Reads in-memory Phase 2 state only — no network I/O.
    """
    with _state_lock:
        in_progress = _check_in_progress
        state = _latest_state

    config, _ = load_update_config()
    installed = config.installed_version if config else ""

    if state is None:
        return {
            "status": "checking" if in_progress else "pending",
            "checking": in_progress,
            "update_available": False,
            "installed_version": installed,
            "available_version": None,
            "mandatory": False,
            "release_notes": "",
            "published_at": None,
            "last_check_at": None,
            "from_cache": False,
            "error": None,
        }

    payload: dict[str, Any] = {
        "status": state.status,
        "checking": in_progress,
        "update_available": bool(state.update_available),
        "installed_version": state.installed_version or installed,
        "available_version": state.available_version,
        "mandatory": bool(state.mandatory),
        "release_notes": state.release_notes or "",
        "published_at": state.published_at,
        "last_check_at": state.last_check_at,
        "from_cache": bool(state.from_cache),
        "error": state.error,
    }
    if state.installer and isinstance(state.installer, dict):
        payload["installer_filename"] = state.installer.get("filename")
    return payload


def force_refresh_update_check(*, delay_s: float = 0.0) -> bool:
    """
    Manual 'Check for updates' — bypasses cache.
    Returns True if a background check was scheduled, False if already in progress.
    """
    if is_update_check_in_progress():
        return False
    schedule_background_update_check(delay_s=delay_s, force=True)
    return True
