# Build AICA.Updater.exe (Phase 5 apply helper — small, no WebView/voice)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root

. (Join-Path $PSScriptRoot "lib\aica_version.ps1")
if (-not $env:AICA_VERSION) {
    $info = Initialize-AicaBuildEnvironment -Root $Root
    $env:AICA_VERSION = $info.Version
    if ($info.Build) { $env:AICA_BUILD = $info.Build }
} else {
    Sync-AicaFileVersionInfo -Version $env:AICA_VERSION -Root $Root | Out-Null
}

$Pip = Join-Path $Root "venv\Scripts\pip.exe"
$PyI = Join-Path $Root "venv\Scripts\pyinstaller.exe"

$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $Pip install -q "pyinstaller>=6.0" 2>&1 | Out-Null
$ErrorActionPreference = $prevEap
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }

Write-Host "==> Cleaning previous updater build"
Remove-Item -Force (Join-Path $Root "dist\AICA.Updater.exe") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $Root "build\AICA.Updater") -ErrorAction SilentlyContinue

$Spec = Join-Path $Root "desktop\packaging\aica_updater.spec"
& $PyI $Spec --noconfirm --distpath (Join-Path $Root "dist") --workpath (Join-Path $Root "build")

$Out = Join-Path $Root "dist\AICA.Updater.exe"
if (-not (Test-Path $Out)) { throw "Updater build failed" }
Write-Host "OK updater -> $Out"
