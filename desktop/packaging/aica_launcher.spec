# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for AICA desktop launcher (WebView2 + voice v2)."""
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]
block_cipher = None

voice_models = ROOT / "desktop" / "voice" / "models"
voice_assets = ROOT / "desktop" / "voice" / "assets"
datas = []
if voice_models.is_dir():
    for sub in voice_models.rglob("*"):
        if sub.is_file():
            rel = sub.relative_to(voice_models)
            datas.append((str(sub), str(Path("desktop") / "voice" / "models" / rel.parent)))
if voice_assets.is_dir():
    for sub in voice_assets.rglob("*"):
        if sub.is_file():
            rel = sub.relative_to(voice_assets)
            datas.append((str(sub), str(Path("desktop") / "voice" / "assets" / rel.parent)))

# faster-whisper Silero VAD (needed if vad_filter=True; bundled for safety)
_fw_assets = ROOT / "venv" / "Lib" / "site-packages" / "faster_whisper" / "assets"
if not _fw_assets.is_dir():
    try:
        import faster_whisper as _fw

        _fw_assets = Path(_fw.__file__).resolve().parent / "assets"
    except Exception:
        _fw_assets = None
if _fw_assets and _fw_assets.is_dir():
    for sub in _fw_assets.rglob("*"):
        if sub.is_file():
            rel = sub.relative_to(_fw_assets)
            datas.append((str(sub), str(Path("faster_whisper") / "assets" / rel.parent)))

hiddenimports = [
    "webview",
    "clr",
    "backend.runtime_paths",
    "desktop.launcher.webview_desktop",
    "desktop.launcher.voice_bridge",
    "desktop.launcher.voice_engine",
    "desktop.launcher.voice_legacy",
    "desktop.launcher.voice_stt",
    "desktop.launcher.voice_wake",
    "desktop.launcher.voice_wake_oww",
    "desktop.launcher.voice_wake_embed",
    "desktop.launcher.voice_audio",
    "desktop.launcher.voice_intents",
    "desktop.launcher.voice_tts",
    "desktop.launcher.voice_paths",
    "desktop.launcher.voice_log",
    "desktop.launcher.voice_diag",
    "faster_whisper",
    "faster_whisper.assets",
    "ctranslate2",
    "onnxruntime",
    "sounddevice",
    "numpy",
    "huggingface_hub",
]

a = Analysis(
    [str(ROOT / "desktop" / "launcher" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "desktop" / "packaging" / "pyi_rth_aica.py")],
    excludes=[
        "torch",
        "torchvision",
        "torchaudio",
        "matplotlib",
        "scipy",
        "sklearn",
        "ultralytics",
        "cv2",
        "tensorboard",
    ],
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
