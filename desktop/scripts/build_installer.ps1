# Full desktop build: engine + launcher + Inno Setup installer → Desktop\AICA\
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root

$Version = "1.0.1"
$env:AICA_VERSION = $Version
$env:AICA_BUILD = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")

# Keep version.json build stamp in sync for About page
$VersionJson = Join-Path $Root "desktop\config\version.json"
if (Test-Path $VersionJson) {
    $vj = Get-Content $VersionJson -Raw | ConvertFrom-Json
    $vj.version = $Version
    $vj.build = $env:AICA_BUILD
    ($vj | ConvertTo-Json -Depth 6) | Set-Content -Encoding UTF8 $VersionJson
}

& (Join-Path $PSScriptRoot "build_engine.ps1")
& (Join-Path $PSScriptRoot "build_launcher.ps1")

$Engine = Join-Path $Root "dist\AICA.Engine\AICA.Engine.exe"
$Launch = Join-Path $Root "dist\AICA.exe"
if (-not (Test-Path $Engine)) { throw "Missing $Engine" }
if (-not (Test-Path $Launch)) { throw "Missing $Launch" }

$Iscc = $null
foreach ($c in @(
    "$env:LocalAppData\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)) {
    if (Test-Path $c) { $Iscc = $c; break }
}

if (-not $Iscc) {
    Write-Warning "Inno Setup 6 (ISCC.exe) not found. Engine+launcher built; installer skipped."
    Write-Host "Artifacts:"
    Write-Host "  $Launch"
    Write-Host "  $Engine"
    exit 0
}

$Iss = Join-Path $Root "desktop\packaging\aica_setup.iss"
Write-Host "==> Inno Setup: $Iscc"
& $Iscc $Iss
$Setup = Join-Path $Root "dist\AICA_Setup_$Version.exe"
if (-not (Test-Path $Setup)) { throw "Installer not produced at $Setup" }
Write-Host "OK installer -> $Setup"

# Official user-facing release folder on Desktop
$DesktopRelease = Join-Path $env:USERPROFILE "Desktop\AICA"
New-Item -ItemType Directory -Force -Path $DesktopRelease | Out-Null
$DestSetup = Join-Path $DesktopRelease "AICA_Setup_$Version.exe"
Copy-Item -Force $Setup $DestSetup

@"
AICA Desktop Release $Version

Product: AICA — Financial Intelligence
Installer: AICA_Setup_$Version.exe
Canonical install: %LOCALAPPDATA%\AICA\
Canonical executable: %LOCALAPPDATA%\AICA\AICA.exe
Canonical engine: %LOCALAPPDATA%\AICA\AICA.Engine.exe
Desktop shortcut: %USERPROFILE%\Desktop\AICA.lnk

This installer upgrades the same AppId installation (does not create parallel copies).

Build time: $($env:AICA_BUILD)
"@ | Set-Content -Encoding UTF8 (Join-Path $DesktopRelease "RELEASE_$Version.txt")

@"
AICA $Version
$($env:AICA_BUILD)
"@ | Set-Content -Encoding UTF8 (Join-Path $DesktopRelease "VERSION.txt")

# Remove obsolete unversioned / old setups from Desktop release folder only
Get-ChildItem $DesktopRelease -Filter "AICA_Setup*.exe" | Where-Object { $_.Name -ne "AICA_Setup_$Version.exe" } | Remove-Item -Force

Write-Host "OK Desktop release -> $DestSetup"
Get-Item $DestSetup | Format-List FullName, Length, LastWriteTime
