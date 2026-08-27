# Full desktop build: engine + launcher + Inno Setup installer -> Desktop\AICA\
# Version source of truth: desktop/config/version.json (never hardcode version here)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root

. (Join-Path $PSScriptRoot "lib\aica_version.ps1")

$buildInfo = Initialize-AicaBuildEnvironment -Root $Root -UpdateBuildStamp
$Version = $buildInfo.Version
$env:AICA_VERSION = $Version
$env:AICA_BUILD = $buildInfo.Build

Write-Host "==> AICA build version $Version (build $($env:AICA_BUILD)) from version.json"

& (Join-Path $PSScriptRoot "build_engine.ps1")
& (Join-Path $PSScriptRoot "build_launcher.ps1")
& (Join-Path $PSScriptRoot "build_updater.ps1")

$Engine = Join-Path $Root "dist\AICA.Engine\AICA.Engine.exe"
$Launch = Join-Path $Root "dist\AICA.exe"
$Updater = Join-Path $Root "dist\AICA.Updater.exe"
if (-not (Test-Path $Engine)) { throw "Missing $Engine" }
if (-not (Test-Path $Launch)) { throw "Missing $Launch" }
if (-not (Test-Path $Updater)) { throw "Missing $Updater" }

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

# Strip dist-info license trees (runtime-unused; deeply nested LICENSE paths break Inno Setup on Windows)
$Internal = Join-Path $Root "dist\AICA.Engine\_internal"
if (Test-Path $Internal) {
    Get-ChildItem $Internal -Directory -Filter "*dist-info" | ForEach-Object {
        $lic = Join-Path $_.FullName "licenses"
        if (Test-Path $lic) {
            Write-Host "==> Removing unused license metadata: $($_.Name)\licenses"
            Remove-Item -Recurse -Force $lic
        }
    }
}

Write-Host "==> Inno Setup: $Iscc /DMyAppVersion=$Version"
& $Iscc "/DMyAppVersion=$Version" $Iss
$Setup = Join-Path $Root "dist\AICA_Setup_$Version.exe"
if (-not (Test-Path $Setup)) { throw "Installer not produced at $Setup" }
Write-Host "OK installer -> $Setup"

# --- Update manifest (REQUIRED for auto-updater; never ship installer alone) ---
$ReleaseNotes = $env:AICA_RELEASE_NOTES
if (-not $ReleaseNotes) {
    $notesFile = Join-Path $Root "desktop\config\release_notes.txt"
    if (Test-Path $notesFile) {
        $ReleaseNotes = (Get-Content $notesFile -Raw -Encoding UTF8).Trim()
    }
}
if (-not $ReleaseNotes) {
    $ReleaseNotes = "AICA $Version desktop release. See the GitHub release page for full notes."
}
$ManifestOut = Join-Path $Root "dist\aica-update-manifest.json"
Write-Host "==> Generating aica-update-manifest.json from final installer"
& (Join-Path $PSScriptRoot "generate_update_manifest.ps1") `
    -InstallerPath $Setup `
    -OutputPath $ManifestOut `
    -ReleaseTag "v$Version" `
    -ReleaseNotes $ReleaseNotes `
    -Channel stable
if (-not (Test-Path $ManifestOut)) { throw "Update manifest was not generated at $ManifestOut" }

& (Join-Path $PSScriptRoot "verify_release_artifacts.ps1") `
    -InstallerPath $Setup `
    -ManifestPath $ManifestOut `
    -ExpectedVersion $Version
Write-Host "OK update manifest -> $ManifestOut"

# Official user-facing release folder on Desktop
$DesktopRelease = Join-Path $env:USERPROFILE "Desktop\AICA"
New-Item -ItemType Directory -Force -Path $DesktopRelease | Out-Null
$DestSetup = Join-Path $DesktopRelease "AICA_Setup_$Version.exe"
$DestManifest = Join-Path $DesktopRelease "aica-update-manifest.json"
Copy-Item -Force $Setup $DestSetup
Copy-Item -Force $ManifestOut $DestManifest

@"
AICA Desktop Release $Version

Product: AICA — Financial Intelligence
Installer: AICA_Setup_$Version.exe
Update manifest: aica-update-manifest.json
Canonical install: %LOCALAPPDATA%\AICA\
Canonical executable: %LOCALAPPDATA%\AICA\AICA.exe
Canonical engine: %LOCALAPPDATA%\AICA\AICA.Engine.exe
Desktop shortcut: %USERPROFILE%\Desktop\AICA.lnk

This installer upgrades the same AppId installation (does not create parallel copies).

GitHub Release checklist (BOTH assets required for auto-update):
  1. Create release tag v$Version
  2. Upload AICA_Setup_$Version.exe
  3. Upload aica-update-manifest.json  <-- clients fetch releases/latest/download/aica-update-manifest.json
  4. Confirm the manifest installer.url matches the uploaded setup asset
  5. Confirm SHA-256 in the manifest matches the uploaded setup file

Build time: $($env:AICA_BUILD)
"@ | Set-Content -Encoding UTF8 (Join-Path $DesktopRelease "RELEASE_$Version.txt")

@"
AICA $Version
$($env:AICA_BUILD)
"@ | Set-Content -Encoding UTF8 (Join-Path $DesktopRelease "VERSION.txt")

# Remove obsolete unversioned / old setups from Desktop release folder only
Get-ChildItem $DesktopRelease -Filter "AICA_Setup*.exe" | Where-Object { $_.Name -ne "AICA_Setup_$Version.exe" } | Remove-Item -Force

Write-Host "OK Desktop release -> $DestSetup"
Write-Host "OK Desktop manifest -> $DestManifest"
Get-Item $DestSetup | Format-List FullName, Length, LastWriteTime
Write-Host ""
Write-Host "REMINDER: GitHub Releases must include BOTH the installer AND aica-update-manifest.json."
