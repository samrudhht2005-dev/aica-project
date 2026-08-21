# Build AICA.exe launcher (WebView2)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root

$Pip = Join-Path $Root "venv\Scripts\pip.exe"
$PyI = Join-Path $Root "venv\Scripts\pyinstaller.exe"

& $Pip install -q "pyinstaller>=6.0" "pywebview>=5.0"

Write-Host "==> Cleaning previous launcher build"
Remove-Item -Force (Join-Path $Root "dist\AICA.exe") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $Root "build\AICA") -ErrorAction SilentlyContinue

$Spec = Join-Path $Root "desktop\packaging\aica_launcher.spec"
& $PyI $Spec --noconfirm --distpath (Join-Path $Root "dist") --workpath (Join-Path $Root "build")

$Out = Join-Path $Root "dist\AICA.exe"
if (-not (Test-Path $Out)) { throw "Launcher build failed" }
Write-Host "OK launcher -> $Out"
