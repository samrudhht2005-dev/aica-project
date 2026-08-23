"""Path and handoff validation for AICA.Updater (fail closed)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}$")
HANDOFF_NAME = "handoff.json"
SCHEMA_VERSION = 1
CHUNK = 1024 * 1024


def updates_staging_root() -> Path:
    base = os.environ.get("TEMP") or tempfile.gettempdir()
    return Path(base).resolve() / "AICA" / "updates"


def expected_installer_filename(version: str) -> str:
    return f"AICA_Setup_{version}.exe"


def staging_dir_for_version(version: str) -> Path:
    return updates_staging_root() / version


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def sha256_file(path: Path, *, chunk_size: int = CHUNK) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class Handoff:
    target_version: str
    sha256: str
    installer_filename: str
    installer_path: Path
    aica_pid: int
    engine_pid: int | None
    install_dir: Path
    restart_exe: Path
    dry_run: bool
    handoff_path: Path


def validate_handoff_path(path: Path) -> str | None:
    try:
        resolved = path.resolve()
    except OSError as e:
        return f"handoff path unreadable: {e}"
    if resolved.name != HANDOFF_NAME:
        return "handoff filename must be handoff.json"
    if not _is_under(resolved, updates_staging_root()):
        return "handoff path outside trusted staging root"
    if not resolved.is_file():
        return "handoff file missing"
    return None


def _validate_install_dir(install_dir: Path) -> str | None:
    try:
        resolved = install_dir.resolve()
    except OSError as e:
        return f"install_dir unreadable: {e}"
    if resolved.name.upper() != "AICA":
        return "install_dir must be an AICA directory"
    # Must live under LocalAppData or an explicit AICA_APPDATA-style tree — not Program Files.
    local = os.environ.get("LOCALAPPDATA")
    if local:
        try:
            if _is_under(resolved, Path(local)):
                return None
        except Exception:
            pass
    # Allow TEMP-based test installs under .../AICA only when explicitly named AICA.
    # Reject system roots.
    forbidden_markers = ("windows", "program files", "program files (x86)")
    low = str(resolved).lower()
    for marker in forbidden_markers:
        if marker in low:
            return "install_dir in forbidden location"
    return None


def _validate_restart_exe(restart_exe: Path, install_dir: Path) -> str | None:
    try:
        resolved = restart_exe.resolve()
        install = install_dir.resolve()
    except OSError as e:
        return f"restart_exe unreadable: {e}"
    if resolved.name.lower() != "aica.exe":
        return "restart_exe must be AICA.exe"
    if not _is_under(resolved, install) and resolved.parent != install:
        return "restart_exe must be inside install_dir"
    return None


def load_and_validate_handoff(handoff_path: Path) -> tuple[Handoff | None, str | None]:
    path_err = validate_handoff_path(handoff_path)
    if path_err:
        return None, path_err

    try:
        data = json.loads(handoff_path.read_text(encoding="utf-8"))
    except Exception:
        return None, "handoff json invalid"

    if not isinstance(data, dict):
        return None, "handoff root must be object"

    if int(data.get("schema_version") or 0) != SCHEMA_VERSION:
        return None, "unsupported handoff schema_version"

    version = str(data.get("target_version") or "").strip()
    if not version or not SEMVER_RE.match(version):
        return None, "invalid target_version"

    sha256 = str(data.get("sha256") or "").strip().lower()
    if not SHA256_HEX_RE.match(sha256):
        return None, "invalid sha256"

    filename = str(data.get("installer_filename") or "").strip()
    expected = expected_installer_filename(version)
    if filename != expected:
        return None, "installer_filename mismatch"

    try:
        aica_pid = int(data.get("aica_pid"))
    except (TypeError, ValueError):
        return None, "invalid aica_pid"
    if aica_pid <= 0:
        return None, "invalid aica_pid"

    engine_raw = data.get("engine_pid")
    engine_pid: int | None
    if engine_raw is None or engine_raw == "":
        engine_pid = None
    else:
        try:
            engine_pid = int(engine_raw)
        except (TypeError, ValueError):
            return None, "invalid engine_pid"
        if engine_pid <= 0:
            return None, "invalid engine_pid"

    install_dir = Path(str(data.get("install_dir") or ""))
    dir_err = _validate_install_dir(install_dir)
    if dir_err:
        return None, dir_err

    restart_exe = Path(str(data.get("restart_exe") or ""))
    restart_err = _validate_restart_exe(restart_exe, install_dir)
    if restart_err:
        return None, restart_err

    staging = staging_dir_for_version(version)
    installer_path = (staging / filename).resolve()
    if not _is_under(installer_path, updates_staging_root()):
        return None, "installer path outside trusted staging root"
    if installer_path.name != expected:
        return None, "installer basename rejected"
    if not installer_path.is_file():
        return None, "staged installer missing"
    try:
        if installer_path.stat().st_size <= 0:
            return None, "staged installer empty"
    except OSError:
        return None, "staged installer unreadable"

    dry_run = bool(data.get("dry_run", False))

    return Handoff(
        target_version=version,
        sha256=sha256,
        installer_filename=filename,
        installer_path=installer_path,
        aica_pid=aica_pid,
        engine_pid=engine_pid,
        install_dir=install_dir.resolve(),
        restart_exe=restart_exe.resolve(),
        dry_run=dry_run,
        handoff_path=handoff_path.resolve(),
    ), None


def reverify_installer(handoff: Handoff) -> str | None:
    """Independent SHA-256 re-check before execution. Returns error or None."""
    if not handoff.installer_path.is_file():
        return "staged installer missing"
    try:
        size = handoff.installer_path.stat().st_size
    except OSError:
        return "staged installer unreadable"
    if size <= 0:
        return "staged installer empty"
    try:
        actual = sha256_file(handoff.installer_path)
    except OSError:
        return "installer rehash failed"
    if not hmac.compare_digest(actual.lower(), handoff.sha256.lower()):
        return "installer sha256 mismatch"
    return None


def build_installer_command(installer_path: Path, install_dir: Path) -> list[str]:
    """Silent Inno flags consistent with desktop/scripts/test_silent_install.ps1.

    /FORCECLOSEAPPLICATIONS is required for unattended updates: with
    CloseApplications=yes and /SUPPRESSMSGBOXES, Inno aborts (exit 5) if it
    cannot close apps holding files. The updater already waited for known PIDs;
    this flag only force-closes processes still locking files being replaced.
    """
    return [
        str(installer_path),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/FORCECLOSEAPPLICATIONS",
        f"/DIR={install_dir}",
    ]


def read_installed_version(install_dir: Path) -> str | None:
    path = install_dir / "version.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    version = str(data.get("version") or "").strip()
    if not SEMVER_RE.match(version):
        return None
    return version


def write_handoff_file(
    *,
    staging_dir: Path,
    target_version: str,
    sha256: str,
    aica_pid: int,
    engine_pid: int | None,
    install_dir: Path,
    restart_exe: Path,
    dry_run: bool = False,
) -> Path:
    """Write handoff.json into an already-trusted staging directory."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    path = staging_dir / HANDOFF_NAME
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "target_version": target_version,
        "sha256": sha256.lower(),
        "installer_filename": expected_installer_filename(target_version),
        "aica_pid": int(aica_pid),
        "engine_pid": engine_pid,
        "install_dir": str(install_dir),
        "restart_exe": str(restart_exe),
        "dry_run": bool(dry_run),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
