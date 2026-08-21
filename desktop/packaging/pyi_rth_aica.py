"""PyInstaller runtime hook — set AICA_ROOT / desktop flags before imports."""
import os
import sys
from pathlib import Path

os.environ.setdefault("AICA_DESKTOP", "1")
os.environ.setdefault("AICA_VERSION", "1.0.0")

if getattr(sys, "frozen", False):
    meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    # Prefer _MEIPASS when it contains frontend/
    if (meipass / "frontend").is_dir():
        os.environ.setdefault("AICA_ROOT", str(meipass))
    else:
        os.environ.setdefault("AICA_ROOT", str(Path(sys.executable).resolve().parent))
