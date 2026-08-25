"""
Runtime path resolution for AICA web + desktop (PyInstaller-safe).

Web/dev: project root next to backend/, frontend/, vision/, database/
Desktop frozen: contents next to the engine executable / _MEIPASS
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


APP_NAME = "AICA"
# Dev fallback only — packaged/desktop releases resolve version from version.json via app_release_info().
APP_VERSION = os.environ.get("AICA_VERSION", "0.0.0-dev")
APP_WINDOW_TITLE = "AICA — Financial Intelligence"


def app_release_info() -> dict:
    """Version + build stamp for About /health (from env or packaged version.json)."""
    version = os.environ.get("AICA_VERSION") or APP_VERSION
    build = os.environ.get("AICA_BUILD") or ""
    channel = "stable"
    try:
        candidates = []
        if is_frozen():
            candidates.append(Path(sys.executable).resolve().parent / "version.json")
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                candidates.append(Path(meipass) / "desktop" / "config" / "version.json")
        candidates.append(project_root() / "desktop" / "config" / "version.json")
        for path in candidates:
            if path.is_file():
                import json
                # utf-8-sig accepts both BOM and BOM-less UTF-8 (Windows writers often add BOM).
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                version = str(data.get("version") or version)
                build = str(data.get("build") or build)
                channel = str(data.get("channel") or channel)
                break
    except Exception:
        pass
    return {"name": APP_NAME, "version": version, "build": build, "channel": channel}

# Template / example values that must NEVER be used as live configuration.
_PLACEHOLDER_DB_RE = re.compile(
    r"(@HOST([:/]|$))|(://\s*USER:PASSWORD@)|(YOUR_.*HERE)|(CHANGE_?ME)|(EXAMPLE\.COM)",
    re.IGNORECASE,
)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _has_frontend_bundle(base: Path) -> bool:
    """True when base contains bundled Jinja templates and static assets."""
    try:
        return (
            (base / "frontend" / "templates").is_dir()
            and (base / "frontend" / "static").is_dir()
        )
    except OSError:
        return False


def _project_root_candidates() -> list[Path]:
    """Ordered search bases for the packaged frontend bundle."""
    candidates: list[Path] = []
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        exe_dir = Path(sys.executable).resolve().parent
        if meipass:
            candidates.append(Path(meipass))
        candidates.append(exe_dir / "_internal")
        candidates.append(exe_dir)
    else:
        candidates.append(Path(__file__).resolve().parent.parent)
    seen: set[Path] = set()
    ordered: list[Path] = []
    for base in candidates:
        try:
            resolved = base.resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            ordered.append(resolved)
    return ordered


def project_root() -> Path:
    """Readable application root (templates, static, vision, database JSON)."""
    override = os.environ.get("AICA_ROOT")
    if override:
        root = Path(override).resolve()
        if _has_frontend_bundle(root):
            return root
    for base in _project_root_candidates():
        if _has_frontend_bundle(base):
            return base
    if override:
        return Path(override).resolve()
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def appdata_dir() -> Path:
    """User-writable config/logs (Windows AppData, else ~/.aica)."""
    override = os.environ.get("AICA_APPDATA")
    if override:
        path = Path(override)
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        path = Path(base) / APP_NAME
    else:
        path = Path.home() / ".aica"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    d = appdata_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_env_path() -> Path:
    """Production/desktop .env — never commit; loaded in addition to project .env."""
    return appdata_dir() / "config.env"


def session_secret_path() -> Path:
    if is_frozen() or os.environ.get("AICA_DESKTOP") == "1":
        return appdata_dir() / "session_secret"
    return project_root() / "database" / ".session_secret"


def templates_dir() -> Path:
    return project_root() / "frontend" / "templates"


def static_dir() -> Path:
    return project_root() / "frontend" / "static"


def vision_weights_dir() -> Path:
    return project_root() / "vision" / "weights"


def tax_rules_path() -> Path:
    return project_root() / "database" / "tax_rules.json"


def is_placeholder_value(key: str, value: str | None) -> bool:
    """True for empty or template/example values (must not be used at runtime)."""
    if value is None:
        return True
    text = str(value).strip().strip('"').strip("'")
    if not text:
        return True
    key_u = (key or "").upper()
    if key_u == "DATABASE_URL":
        if _PLACEHOLDER_DB_RE.search(text):
            return True
        # Literal hostname HOST in a postgres URL
        if re.search(r"@HOST(?::\d+)?/", text, re.IGNORECASE):
            return True
        if "://" not in text:
            return True
        return False
    if key_u in ("GEMINI_API_KEY", "AICA_SECRET_KEY"):
        upper = text.upper()
        return upper.startswith("YOUR_") or "CHANGE_ME" in upper or upper.endswith("_HERE")
    return False


def is_sqlite_url(url: str | None) -> bool:
    if not url:
        return False
    return url.strip().lower().startswith("sqlite:")


def is_valid_database_url(url: str | None) -> bool:
    if not url or is_placeholder_value("DATABASE_URL", url):
        return False
    text = url.strip().lower()
    return text.startswith(("postgresql://", "postgres://", "sqlite:"))


def experimental_sqlite_url() -> str:
    """Repo-local SQLite file for automated experiments (never the packaged user DB)."""
    override = os.environ.get("AICA_SQLITE_PATH")
    if override:
        path = Path(override)
    else:
        path = project_root() / "database" / "_experiment" / "aica_experiment.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return "sqlite:///" + path.resolve().as_posix()


def desktop_sqlite_url() -> str:
    """Persistent per-user SQLite file under %AppData%\\AICA\\ (or AICA_APPDATA)."""
    path = appdata_dir() / "aica.db"
    return "sqlite:///" + path.resolve().as_posix()


def resolve_database_url() -> str | None:
    """Return a usable DATABASE_URL, or None if missing/placeholder."""
    url = os.environ.get("DATABASE_URL")
    if is_valid_database_url(url):
        return url.strip().strip('"').strip("'")
    backend = (os.environ.get("AICA_DB_BACKEND") or "").strip().lower()
    if backend in ("postgres", "postgresql"):
        return None
    if backend in ("sqlite", "experiment"):
        return experimental_sqlite_url()
    # Packaged desktop: SQLite by default (no PostgreSQL required).
    if is_frozen():
        return desktop_sqlite_url()
    return None


def database_config_error_message() -> str:
    cfg = config_env_path()
    return (
        "AICA could not start: DATABASE_URL is missing or still a placeholder.\n\n"
        f"Edit:\n  {cfg}\n\n"
        "Set a PostgreSQL URL (web/development), for example:\n"
        "  DATABASE_URL=postgresql://USER:PASSWORD@hostname:5432/aica_db\n"
        "Packaged desktop defaults to a local SQLite file:\n"
        f"  {appdata_dir() / 'aica.db'}\n\n"
        "Development: use the project .env (never commit it).\n"
        "Installed desktop: optional overrides in %AppData%\\AICA\\config.env — "
        "do not leave USER/PASSWORD/HOST template values."
    )


def _apply_dotenv_file(path: Path, *, override: bool) -> None:
    """Apply only non-placeholder keys from an env file."""
    try:
        from dotenv import dotenv_values
    except ImportError:
        return
    if not path.is_file():
        return
    values = dotenv_values(path)
    for key, raw in values.items():
        if not key or raw is None:
            continue
        val = str(raw).strip()
        if is_placeholder_value(key, val):
            continue
        existing = os.environ.get(key)
        if existing and not is_placeholder_value(key, existing) and not override:
            continue
        # Never let a placeholder file value replace a good process env value
        if existing and not is_placeholder_value(key, existing) and is_placeholder_value(key, val):
            continue
        if override or not existing or is_placeholder_value(key, existing):
            os.environ[key] = val


def _scrub_placeholder_env() -> None:
    """Remove placeholder values that may already be in the process environment."""
    for key in ("DATABASE_URL", "GEMINI_API_KEY", "AICA_SECRET_KEY"):
        if key in os.environ and is_placeholder_value(key, os.environ.get(key)):
            del os.environ[key]


def scrub_appdata_placeholder_config() -> None:
    """
    If %AppData%\\AICA\\config.env still contains template DATABASE_URL=@HOST,
    comment those lines out so they cannot override a real configuration.
    Does not print secret values.
    """
    path = config_env_path()
    if not path.is_file():
        return
    try:
        original = path.read_text(encoding="utf-8")
    except OSError:
        return
    changed = False
    out_lines: list[str] = []
    for line in original.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out_lines.append(line)
            continue
        key, _, val = stripped.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if is_placeholder_value(key, val):
            out_lines.append(f"# (placeholder removed — set a real value) {stripped}")
            changed = True
        else:
            out_lines.append(line)
    if changed:
        try:
            path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        except OSError:
            pass


def ensure_appdata_config_template() -> None:
    """Create an empty commented config.env if missing (no live placeholder URLs)."""
    path = config_env_path()
    if path.is_file():
        scrub_appdata_placeholder_config()
        return
    path.write_text(
        "# AICA desktop configuration\n"
        "# Fill in real values. Do not leave USER / PASSWORD / HOST placeholders.\n"
        "#\n"
        "# Desktop uses SQLite by default (aica.db in this folder).\n"
        "# Optional hosted PostgreSQL:\n"
        "# DATABASE_URL=postgresql://USER:PASSWORD@your-db-host:5432/aica_db\n"
        "# GEMINI_API_KEY=\n"
        "# AICA_SECRET_KEY=\n"
        "AICA_DESKTOP=1\n"
        f"AICA_VERSION={APP_VERSION}\n",
        encoding="utf-8",
    )


def load_runtime_env() -> None:
    """
    Load configuration with safe precedence:

    1. Existing valid process environment (highest — scripts / launcher)
    2. %AppData%\\AICA\\config.env (user/production) — valid keys only
    3. Optional config.env beside the executable
    4. Project .env (development) — fills gaps only
    5. No fake DATABASE_URL fallback

    Placeholder template values (USER:PASSWORD@HOST, YOUR_*_HERE) are ignored.
    """
    try:
        from dotenv import load_dotenv  # noqa: F401 — availability check
    except ImportError:
        _scrub_placeholder_env()
        return

    ensure_appdata_config_template()
    scrub_appdata_placeholder_config()

    # Capture pre-existing valid process env (must win over files if already set by launcher/tests)
    preset_db = os.environ.get("DATABASE_URL")
    preset_db_valid = is_valid_database_url(preset_db)

    # Dev .env first as baseline (no override of existing)
    root_env = project_root() / ".env"
    _apply_dotenv_file(root_env, override=False)

    # User/production AppData (valid keys override)
    _apply_dotenv_file(config_env_path(), override=True)

    if is_frozen():
        beside = Path(sys.executable).resolve().parent / "config.env"
        _apply_dotenv_file(beside, override=True)
        # Also check install root (launcher dir) when engine lives in engine\
        parent_cfg = Path(sys.executable).resolve().parent.parent / "config.env"
        _apply_dotenv_file(parent_cfg, override=True)
        os.environ.setdefault("AICA_DESKTOP", "1")
        os.environ.setdefault("AICA_VERSION", APP_VERSION)

    # Optional explicit file (CI / advanced)
    env_file = os.environ.get("AICA_ENV_FILE")
    if env_file:
        _apply_dotenv_file(Path(env_file), override=True)

    _scrub_placeholder_env()

    # Restore process DATABASE_URL if it was valid before file loads (scripts/launcher)
    if preset_db_valid and preset_db:
        os.environ["DATABASE_URL"] = preset_db
