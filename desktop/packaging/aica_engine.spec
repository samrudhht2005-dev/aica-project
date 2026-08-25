# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for AICA.Engine (FastAPI + CV sidecar).

Build:
  .\\venv\\Scripts\\pyinstaller.exe desktop\\packaging\\aica_engine.spec --noconfirm
"""
import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]

block_cipher = None

datas = [
    (str(ROOT / "frontend" / "templates"), "frontend/templates"),
    (str(ROOT / "frontend" / "static"), "frontend/static"),
    (str(ROOT / "database" / "tax_rules.json"), "database"),
    (str(ROOT / "vision" / "detector_config.json"), "vision"),
    (str(ROOT / "vision" / "product_classes.py"), "vision"),
    (str(ROOT / "desktop" / "config" / "version.json"), "desktop/config"),
]

# Bundle YOLO weights if present
weights = ROOT / "vision" / "weights" / "aica_product_detector.pt"
if weights.is_file():
    datas.append((str(weights), "vision/weights"))

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

a = Analysis(
    [str(ROOT / "desktop" / "engine_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "desktop" / "packaging" / "pyi_rth_aica.py")],
    excludes=["tkinter", "matplotlib.tests", "tests"],
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
