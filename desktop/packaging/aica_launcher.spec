# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for AICA desktop launcher (WebView2)."""
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]
block_cipher = None

a = Analysis(
    [str(ROOT / "desktop" / "launcher" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=["webview", "clr", "backend.runtime_paths", "desktop.launcher.webview_desktop", "desktop.launcher.voice_bridge"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="AICA",
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
    icon=str(ROOT / "desktop" / "assets" / "aica.ico") if (ROOT / "desktop" / "assets" / "aica.ico").is_file() else None,
)
