# Build AICA.Engine (FastAPI + CV) with PyInstaller
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root

. (Join-Path $PSScriptRoot "lib\aica_version.ps1")
if (-not $env:AICA_VERSION) {
    $info = Initialize-AicaBuildEnvironment -Root $Root
    $env:AICA_VERSION = $info.Version
    if ($info.Build) { $env:AICA_BUILD = $info.Build }
}

$Py = Join-Path $Root "venv\Scripts\python.exe"
$Pip = Join-Path $Root "venv\Scripts\pip.exe"
$PyI = Join-Path $Root "venv\Scripts\pyinstaller.exe"

if (-not (Test-Path $Py)) { throw "venv python not found. Create venv and install requirements first." }

Write-Host "==> Verifying CPU Torch / torchvision / ultralytics (required for Engine)"
& $Py -c @"
import sys
try:
    import torch
except ImportError:
    print('MISSING torch - install CPU wheels:')
    print(r'  .\venv\Scripts\pip.exe install -r desktop\requirements-engine.txt --index-url https://download.pytorch.org/whl/cpu')
    sys.exit(2)
ver = getattr(torch, '__version__', '')
cuda = getattr(torch.version, 'cuda', None)
if ('+cu' in ver.lower()) or (cuda not in (None, '') and '+cpu' not in ver.lower()):
    print(f'REJECTED CUDA torch build: {ver!r} cuda={cuda!r}')
    print('Uninstall and reinstall from desktop/requirements-engine.txt (CPU index).')
    sys.exit(2)
try:
    import torchvision
    import ultralytics
except ImportError as e:
    print('MISSING dependency:', e)
    sys.exit(2)
from pathlib import Path
w = Path('vision/weights/aica_product_detector.pt')
if not w.is_file():
    print('MISSING weights:', w.resolve())
    sys.exit(2)
print(f'OK torch={ver} torchvision={torchvision.__version__} ultralytics={ultralytics.__version__}')
print(f'OK weights={w} bytes={w.stat().st_size}')
"@
if ($LASTEXITCODE -ne 0) { throw "Engine prerequisites failed (exit $LASTEXITCODE). See desktop/requirements-engine.txt." }

$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $Pip install -q "pyinstaller>=6.0" "pywebview>=5.0" 2>&1 | Out-Null
$ErrorActionPreference = $prevEap
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }

Write-Host "==> Cleaning previous engine build"
Remove-Item -Recurse -Force (Join-Path $Root "build\AICA.Engine") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $Root "dist\AICA.Engine") -ErrorAction SilentlyContinue

$Spec = Join-Path $Root "desktop\packaging\aica_engine.spec"
Write-Host "==> PyInstaller engine (includes CPU torch - this can take several minutes)"
& $PyI $Spec --noconfirm --distpath (Join-Path $Root "dist") --workpath (Join-Path $Root "build")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller engine failed (exit $LASTEXITCODE)" }

$Out = Join-Path $Root "dist\AICA.Engine\AICA.Engine.exe"
if (-not (Test-Path $Out)) { throw "Engine build failed: $Out missing" }

# Post-build: Torch must be present inside the onedir bundle
$TorchDir = Join-Path $Root "dist\AICA.Engine\_internal\torch"
if (-not (Test-Path $TorchDir)) {
    throw "Packaged Engine is missing _internal\torch - collect_all(torch) failed."
}
$PackagedWeights = Join-Path $Root "dist\AICA.Engine\_internal\vision\weights\aica_product_detector.pt"
if (-not (Test-Path $PackagedWeights)) {
    throw "Packaged Engine is missing vision weights at $PackagedWeights"
}

Write-Host "OK engine -> $Out"
Write-Host "OK bundled torch -> $TorchDir"
Write-Host "OK bundled weights -> $PackagedWeights"
