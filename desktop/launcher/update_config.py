"""AICA auto-update configuration from version.json (PyInstaller-safe)."""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from backend.runtime_paths import app_release_info, is_frozen, project_root

# Phase 1 approved download/manifest hosts (GitHub Releases only).
ALLOWED_UPDATE_HOSTS = frozenset({"github.com", "objects.githubusercontent.com"})

UPDATE_STRATEGY_GITHUB = "github_releases"

# Release versions: major.minor.patch (optional -pre/+build suffix allowed in manifest).
SEMVER_RELEASE_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)

INSTALLER_FILENAME_RE = re.compile(
    r"^AICA_Setup_(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\.exe$"
)

SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}$")

VALID_CHANNELS = frozenset({"stable", "beta", "prerelease"})


@dataclass(frozen=True)
class UpdateConfig:
    strategy: str
    repo: str
    manifest_url: str
    installed_version: str
    channel: str


def _version_json_candidates() -> list[Path]:
    candidates: list[Path] = []
    if is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "version.json")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "desktop" / "config" / "version.json")
    candidates.append(project_root() / "desktop" / "config" / "version.json")
    return candidates


def load_version_json() -> dict[str, Any] | None:
    for path in _version_json_candidates():
        try:
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
    return None


def _validate_https_url(url: str, *, field: str) -> str | None:
    url = (url or "").strip()
    if not url:
        return f"{field} is empty"
    if not url.lower().startswith("https://"):
        return f"{field} must use HTTPS"
    try:
        parsed = urlparse(url)
    except Exception:
        return f"{field} is not a valid URL"
    host = (parsed.hostname or "").lower()
    if not host:
        return f"{field} has no host"
    if host not in ALLOWED_UPDATE_HOSTS:
        return f"{field} host not allowlisted: {host}"
    return None


def load_update_config() -> tuple[UpdateConfig | None, str | None]:
    """
    Load and validate update settings from version.json.
    Returns (config, error). On error config is None.
    """
    data = load_version_json()
    if not data:
        return None, "version.json not found"

    rel = app_release_info()
    installed = str(rel.get("version") or "").strip()
    if not installed or not SEMVER_RELEASE_RE.match(installed):
        return None, f"invalid installed version: {installed!r}"

    channel = str(data.get("channel") or rel.get("channel") or "stable").strip().lower()
    if channel not in VALID_CHANNELS:
        return None, f"invalid application channel: {channel!r}"

    update = data.get("update")
    if not isinstance(update, dict):
        return None, "update section missing in version.json"

    strategy = str(update.get("strategy") or "").strip()
    if strategy != UPDATE_STRATEGY_GITHUB:
        return None, f"unsupported update strategy: {strategy!r}"

    repo = str(update.get("repo") or "").strip()
    if not repo or "/" not in repo:
        return None, "update.repo is missing or invalid"

    manifest_url = str(update.get("manifest_url") or "").strip()
    err = _validate_https_url(manifest_url, field="update.manifest_url")
    if err:
        return None, err

    return UpdateConfig(
        strategy=strategy,
        repo=repo,
        manifest_url=manifest_url,
        installed_version=installed,
        channel=channel,
    ), None


def expected_installer_filename(version: str) -> str:
    return f"AICA_Setup_{version}.exe"
