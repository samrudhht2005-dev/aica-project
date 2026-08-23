"""PyInstaller runtime hook — set AICA_ROOT / desktop flags before imports."""
import os
import sys
from pathlib import Path

os.environ.setdefault("AICA_DESKTOP", "1")
os.environ.setdefault("AICA_VERSION", "1.0.2")
os.environ.setdefault("AICA_PERSONAL_WAKE", "1")

if getattr(sys, "frozen", False):
    meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    # Prefer _MEIPASS when it contains frontend/
    if (meipass / "frontend").is_dir():
        os.environ.setdefault("AICA_ROOT", str(meipass))
    else:
        os.environ.setdefault("AICA_ROOT", str(Path(sys.executable).resolve().parent))
    # Interactive click-to-talk needs multi-core CTranslate2. A single thread made
    # ~2.8s of speech take ~14–15s of Whisper inference on packaged Desktop.
    _n = os.cpu_count() or 4
    _threads = str(max(2, min(4, max(1, _n - 1))))
    os.environ.setdefault("AICA_WHISPER_THREADS", _threads)
    os.environ.setdefault("OMP_NUM_THREADS", _threads)
    os.environ.setdefault("MKL_NUM_THREADS", _threads)
