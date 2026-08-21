# Build AICA.Engine (FastAPI + CV) with PyInstaller
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root

$Py = Join-Path $Root "venv\Scripts\python.exe"
$Pip = Join-Path $Root "venv\Scripts\pip.exe"
$PyI = Join-Path $Root "venv\Scripts\pyinstaller.exe"

if (-not (Test-Path $Py)) { throw "venv python not found. Create venv and install requirements first." }

& $Pip install -q "pyinstaller>=6.0" "pywebview>=5.0"

Write-Host "==> Cleaning previous engine build"
Remove-Item -Recurse -Force (Join-Path $Root "build\AICA.Engine") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $Root "dist\AICA.Engine") -ErrorAction SilentlyContinue

$Spec = Join-Path $Root "desktop\packaging\aica_engine.spec"
Write-Host "==> PyInstaller engine"
& $PyI $Spec --noconfirm --distpath (Join-Path $Root "dist") --workpath (Join-Path $Root "build")

$Out = Join-Path $Root "dist\AICA.Engine\AICA.Engine.exe"
if (-not (Test-Path $Out)) { throw "Engine build failed: $Out missing" }
Write-Host "OK engine -> $Out"
