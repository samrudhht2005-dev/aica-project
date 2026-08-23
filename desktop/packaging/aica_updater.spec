# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for AICA.Updater.exe (Phase 5).

Small standalone helper: wait for AICA exit → run verified staged installer → restart.

Build (do not run during Phase 5 validation unless explicitly requested):
  .\\venv\\Scripts\\pyinstaller.exe desktop\\packaging\\aica_updater.spec --noconfirm
"""
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]
block_cipher = None

a = Analysis(
    [str(ROOT / "desktop" / "updater" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "desktop.updater",
        "desktop.updater.main",
        "desktop.updater.updater_apply",
        "desktop.updater.updater_validate",
        "desktop.updater.updater_log",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "webview",
        "numpy",
        "torch",
        "faster_whisper",
        "sounddevice",
        "onnxruntime",
        "cv2",
        "ultralytics",
        "sqlalchemy",
        "uvicorn",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

_version_info = ROOT / "desktop" / "packaging" / "file_version_info_updater.txt"
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="AICA.Updater",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(_version_info) if _version_info.is_file() else None,
    icon=str(ROOT / "desktop" / "assets" / "aica.ico") if (ROOT / "desktop" / "assets" / "aica.ico").is_file() else None,
)
