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

$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $Pip install -q "pyinstaller>=6.0" "pywebview>=5.0" 2>&1 | Out-Null
$ErrorActionPreference = $prevEap
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }

Write-Host "==> Cleaning previous engine build"
Remove-Item -Recurse -Force (Join-Path $Root "build\AICA.Engine") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $Root "dist\AICA.Engine") -ErrorAction SilentlyContinue

$Spec = Join-Path $Root "desktop\packaging\aica_engine.spec"
Write-Host "==> PyInstaller engine"
& $PyI $Spec --noconfirm --distpath (Join-Path $Root "dist") --workpath (Join-Path $Root "build")

$Out = Join-Path $Root "dist\AICA.Engine\AICA.Engine.exe"
if (-not (Test-Path $Out)) { throw "Engine build failed: $Out missing" }
Write-Host "OK engine -> $Out"
