# Build AICA.exe launcher (WebView2)
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

if (-not (Test-Path $Py)) { throw "venv python not found." }

Write-Host "==> Ensuring Piper Amy voice (required for launcher / IRA TTS)"
& $Py (Join-Path $Root "desktop\scripts\setup_piper_voice.py")
if ($LASTEXITCODE -ne 0) { throw "Piper Amy setup/download failed (exit $LASTEXITCODE)" }
& $Py (Join-Path $Root "desktop\scripts\setup_piper_voice.py") --verify-only --require-espeak
if ($LASTEXITCODE -ne 0) { throw "Piper Amy verification failed - refuse to build launcher without Amy." }

$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $Pip install -q "pyinstaller>=6.0" "pywebview>=5.0" 2>&1 | Out-Null
$ErrorActionPreference = $prevEap
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }

Write-Host "==> Cleaning previous launcher build"
Remove-Item -Force (Join-Path $Root "dist\AICA.exe") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $Root "build\AICA") -ErrorAction SilentlyContinue

$Spec = Join-Path $Root "desktop\packaging\aica_launcher.spec"
& $PyI $Spec --noconfirm --distpath (Join-Path $Root "dist") --workpath (Join-Path $Root "build")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller launcher failed (exit $LASTEXITCODE)" }

$Out = Join-Path $Root "dist\AICA.exe"
if (-not (Test-Path $Out)) { throw "Launcher build failed" }
Write-Host "OK launcher -> $Out"
