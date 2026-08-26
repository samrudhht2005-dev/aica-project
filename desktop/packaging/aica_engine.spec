# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for AICA.Engine (FastAPI + CV sidecar).

Build:
  .\\venv\\Scripts\\pip.exe install -r desktop\\requirements-engine.txt --index-url https://download.pytorch.org/whl/cpu
  .\\venv\\Scripts\\pyinstaller.exe desktop\\packaging\\aica_engine.spec --noconfirm

CPU-only Torch is mandatory. CUDA wheels are rejected at build time.
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parents[1]

block_cipher = None

# ---------------------------------------------------------------------------
# Fail-fast: CPU torch + trained weights (never ship Engine without AI camera)
# ---------------------------------------------------------------------------
try:
    import torch as _torch
except ImportError as exc:
    raise SystemExit(
        "AICA.Engine build requires torch. Install CPU wheels first:\n"
        "  .\\venv\\Scripts\\pip.exe install -r desktop\\requirements-engine.txt "
        "--index-url https://download.pytorch.org/whl/cpu\n"
        f"Import error: {exc}"
    ) from exc

_tv = str(getattr(_torch, "__version__", "") or "")
_cuda_build = ("+cu" in _tv.lower()) or (getattr(_torch.version, "cuda", None) not in (None, ""))
if _cuda_build and "+cpu" not in _tv.lower():
    raise SystemExit(
        f"AICA.Engine requires CPU-only torch (found {_tv!r}). "
        "Uninstall CUDA torch and install desktop/requirements-engine.txt from the CPU index."
    )

try:
    import torchvision as _torchvision  # noqa: F401
except ImportError as exc:
    raise SystemExit(
        "AICA.Engine build requires torchvision (CPU). "
        "Install desktop/requirements-engine.txt from the PyTorch CPU index.\n"
        f"Import error: {exc}"
    ) from exc

try:
    import ultralytics as _ultralytics  # noqa: F401
except ImportError as exc:
    raise SystemExit(
        f"AICA.Engine build requires ultralytics. Import error: {exc}"
    ) from exc

weights = ROOT / "vision" / "weights" / "aica_product_detector.pt"
if not weights.is_file():
    raise SystemExit(
        f"Missing trained weights required for release: {weights}\n"
        "Do not build AICA.Engine without vision/weights/aica_product_detector.pt."
    )

datas = [
    (str(ROOT / "frontend" / "templates"), "frontend/templates"),
    (str(ROOT / "frontend" / "static"), "frontend/static"),
    (str(ROOT / "database" / "tax_rules.json"), "database"),
    (str(ROOT / "vision" / "detector_config.json"), "vision"),
    (str(ROOT / "vision" / "product_classes.py"), "vision"),
    (str(ROOT / "desktop" / "config" / "version.json"), "desktop/config"),
    (str(weights), "vision/weights"),
]

binaries = []
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "sqlalchemy.dialects.postgresql",
    "sqlalchemy.dialects.sqlite",
    "psycopg2",
    "cv2",
    "torch",
    "torchvision",
    "ultralytics",
    "backend.main",
    "backend.routes",
    "backend.auth",
    "backend.runtime_paths",
    "backend.money",
    "backend.optimization_actions",
    "backend.optimization_sanitize",
    # v1.0.7 Weigh & QR — explicit so PyInstaller does not miss label/QR deps
    "backend.weigh_tickets",
    "backend.weigh_label",
    "backend.product_types",
    "qrcode",
    "qrcode.image.pil",
    "PIL",
    "PIL.Image",
    "fpdf",
    "database.db",
    "database.schema_upgrade",
    "models.db_models",
    "gemini.client",
    "vision.yolo_inference",
    "vision.product_classes",
    "camera.camera_stream",
    "multipart",
    "email_validator",
]

# Pull Torch / torchvision / ultralytics trees (DLLs + package data). Targeted collect_all —
# do not collect unrelated stacks (whisper/piper live in the launcher).
for _pkg in ("torch", "torchvision", "ultralytics"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += list(_h)

a = Analysis(
    [str(ROOT / "desktop" / "engine_entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "desktop" / "packaging" / "pyi_rth_aica.py")],
    excludes=["tkinter", "matplotlib.tests", "tests", "torchaudio"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AICA.Engine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # no console window for end users
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "desktop" / "assets" / "aica.ico") if (ROOT / "desktop" / "assets" / "aica.ico").is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AICA.Engine",
)
