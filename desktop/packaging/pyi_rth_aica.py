"""PyInstaller runtime hook — set AICA_ROOT / desktop flags before imports."""
import os
import sys
from pathlib import Path

os.environ.setdefault("AICA_DESKTOP", "1")
# AICA_VERSION is set by build env / launcher / version.json — no hardcoded release version here.
os.environ.setdefault("AICA_PERSONAL_WAKE", "1")

if getattr(sys, "frozen", False):
    exe_dir = Path(sys.executable).resolve().parent
    meipass = Path(getattr(sys, "_MEIPASS", exe_dir))
    # Resolve packaged assets: Inno flattens engine + _internal into %LOCALAPPDATA%\\AICA\\.
    root = None
    for base in (meipass, exe_dir / "_internal", exe_dir):
        templates = base / "frontend" / "templates"
        static = base / "frontend" / "static"
        if templates.is_dir() and static.is_dir():
            root = base
            break
    if root is not None:
        # Force over inherited launcher AICA_ROOT (launcher bundle has no frontend/).
        os.environ["AICA_ROOT"] = str(root)
    elif os.environ.get("AICA_ROOT"):
        # Drop invalid inherited root so runtime_paths can scan _MEIPASS / _internal.
        del os.environ["AICA_ROOT"]
    # Interactive click-to-talk needs multi-core CTranslate2. A single thread made
    # ~2.8s of speech take ~14–15s of Whisper inference on packaged Desktop.
    _n = os.cpu_count() or 4
    _threads = str(max(2, min(4, max(1, _n - 1))))
    os.environ.setdefault("AICA_WHISPER_THREADS", _threads)
    os.environ.setdefault("OMP_NUM_THREADS", _threads)
    os.environ.setdefault("MKL_NUM_THREADS", _threads)
