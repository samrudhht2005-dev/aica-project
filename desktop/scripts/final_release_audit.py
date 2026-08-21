"""Final release secret/path audit for packaged desktop artifacts. No secrets printed."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "dist" / "AICA.Engine"
SETUP = ROOT / "dist" / "AICA_Setup_1.0.0.exe"
LAUNCHER = ROOT / "dist" / "AICA.exe"


def main() -> int:
    checks = []

    checks.append(("installer_exists", SETUP.is_file()))
    checks.append(("launcher_exists", LAUNCHER.is_file()))
    checks.append(("engine_exists", (ENGINE / "AICA.Engine.exe").is_file()))
    weights = ENGINE / "_internal" / "vision" / "weights" / "aica_product_detector.pt"
    checks.append(("yolo_weights", weights.is_file() and weights.stat().st_size > 1_000_000))

    # Engine newer than or equal packaging window: installer should be after engine
    eng_t = (ENGINE / "AICA.Engine.exe").stat().st_mtime
    setup_t = SETUP.stat().st_mtime
    checks.append(("installer_built_after_engine", setup_t >= eng_t - 5))  # small clock skew OK

    # Forbidden files in package
    forbidden_names = {".env", ".session_secret", "config.env"}
    found_forbidden = []
    for p in ENGINE.rglob("*"):
        if p.is_file() and p.name in forbidden_names:
            found_forbidden.append(str(p))
    checks.append(("no_env_or_secrets_files", len(found_forbidden) == 0))

    # Scan text assets for embedded live secrets
    patterns = [
        ("gemini_assignment", re.compile(rb"GEMINI_API_KEY\s*=\s*[A-Za-z0-9_\-]{16,}")),
        ("google_api_shape", re.compile(rb"AIza[0-9A-Za-z_\-]{20,}")),
        ("db_url_assignment", re.compile(rb"DATABASE_URL\s*=\s*postgresql://[^:\s]+:[^@\s]+@")),
        ("dev_path_hardcode", re.compile(rb"C:\\Users\\Samrudh\\Downloads\\aica-project", re.I)),
    ]
    text_ext = {".py", ".json", ".txt", ".md", ".html", ".js", ".css", ".example", ".yml", ".yaml", ".ini", ".cfg"}
    hits = []
    scan_files = [LAUNCHER, SETUP, ENGINE / "AICA.Engine.exe"]
    for p in ENGINE.rglob("*"):
        if p.is_file() and p.suffix.lower() in text_ext and p.stat().st_size < 2_000_000:
            scan_files.append(p)

    for p in scan_files:
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if p.suffix.lower() in {".exe", ".dll"} and len(data) > 4_000_000:
            data = data[:1_500_000] + data[-1_500_000:]
        for name, rx in patterns:
            if rx.search(data):
                # Allow example/docs mentioning path only in docs outside engine — mark hit
                hits.append((name, str(p.relative_to(ROOT) if p.is_relative_to(ROOT) else p)))

    # Filter: config.env.example with commented keys is OK if no live assignment matched
    checks.append(("no_secret_pattern_hits", len(hits) == 0))
    checks.append(("installer_portable_copy", Path.home().joinpath("Desktop", "AICA_Setup_1.0.0.exe").is_file()))

    # Console subsystem: PyInstaller windowed = no console. Check PE characteristics lightly via string absence of AllocConsole is weak;
    # rely on console=False in specs (already verified by no console during e2e).
    checks.append(("engine_console_false_in_spec", "console=False" in (ROOT / "desktop" / "packaging" / "aica_engine.spec").read_text(encoding="utf-8")))
    checks.append(("launcher_console_false_in_spec", "console=False" in (ROOT / "desktop" / "packaging" / "aica_launcher.spec").read_text(encoding="utf-8")))

    print("AUDIT")
    failed = 0
    for name, ok in checks:
        print(("PASS" if ok else "FAIL"), name)
        if not ok:
            failed += 1
    if hits:
        print("HITS")
        for h in hits[:40]:
            print(" ", h[0], h[1])
    if found_forbidden:
        print("FORBIDDEN", found_forbidden)
    print("installer_mb", round(SETUP.stat().st_size / 1e6, 1))
    print("engine_mtime", eng_t)
    print("setup_mtime", setup_t)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
