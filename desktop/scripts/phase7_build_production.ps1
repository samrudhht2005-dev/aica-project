# Phase 7: production AICA v1.0.3 build (production AppId only).
# Does NOT install to %LOCALAPPDATA%\AICA. Copies installer to dist/ only.
# Does NOT commit, push, or tag.
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root

. (Join-Path $PSScriptRoot "lib\aica_version.ps1")

$Log = Join-Path $Root "dist\phase7_build_production.log"
function Write-Log($msg) {
    $line = "$(Get-Date -Format o) $msg"
    Add-Content -Path $Log -Value $line
    Write-Host $msg
}

# Production safety snapshot
$ProdExe = Join-Path $env:LOCALAPPDATA "AICA\AICA.exe"
$ProdVer = Join-Path $env:LOCALAPPDATA "AICA\version.json"
@{
    prod_exe_exists = (Test-Path $ProdExe)
    prod_exe_len    = if (Test-Path $ProdExe) { (Get-Item $ProdExe).Length } else { $null }
    prod_version    = if (Test-Path $ProdVer) { (Get-Content $ProdVer -Raw) } else { $null }
} | ConvertTo-Json | Set-Content (Join-Path $Root "dist\phase7_prod_snapshot_before.json") -Encoding UTF8

$buildInfo = Initialize-AicaBuildEnvironment -Root $Root -UpdateBuildStamp
$Version = $buildInfo.Version
if ($Version -ne "1.0.3") {
    throw "Expected version 1.0.3 in version.json, found $Version"
}
$env:AICA_VERSION = $Version
$env:AICA_BUILD = $buildInfo.Build
Write-Log "Building AICA $Version (build $($env:AICA_BUILD))"

Write-Log "==> build_engine.ps1"
& (Join-Path $PSScriptRoot "build_engine.ps1") 2>&1 | Tee-Object -FilePath $Log -Append
Write-Log "==> build_launcher.ps1"
& (Join-Path $PSScriptRoot "build_launcher.ps1") 2>&1 | Tee-Object -FilePath $Log -Append
Write-Log "==> build_updater.ps1"
& (Join-Path $PSScriptRoot "build_updater.ps1") 2>&1 | Tee-Object -FilePath $Log -Append

$Engine = Join-Path $Root "dist\AICA.Engine\AICA.Engine.exe"
$Launch = Join-Path $Root "dist\AICA.exe"
$Updater = Join-Path $Root "dist\AICA.Updater.exe"
foreach ($p in @($Engine, $Launch, $Updater)) {
    if (-not (Test-Path $p)) { throw "Missing $p" }
}

$Iscc = $null
foreach ($c in @(
    "$env:LocalAppData\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)) {
    if (Test-Path $c) { $Iscc = $c; break }
}
if (-not $Iscc) { throw "Inno Setup 6 (ISCC.exe) not found" }

$Iss = Join-Path $Root "desktop\packaging\aica_setup.iss"
Write-Log "==> ISCC production aica_setup.iss /DMyAppVersion=$Version"
& $Iscc "/DMyAppVersion=$Version" $Iss 2>&1 | Tee-Object -FilePath $Log -Append
$Setup = Join-Path $Root "dist\AICA_Setup_$Version.exe"
if (-not (Test-Path $Setup)) { throw "Missing $Setup" }
Write-Log "OK production installer -> $Setup size=$((Get-Item $Setup).Length)"

# Post-build safety check
@{
    prod_exe_exists = (Test-Path $ProdExe)
    prod_exe_len    = if (Test-Path $ProdExe) { (Get-Item $ProdExe).Length } else { $null }
    prod_version    = if (Test-Path $ProdVer) { (Get-Content $ProdVer -Raw) } else { $null }
} | ConvertTo-Json | Set-Content (Join-Path $Root "dist\phase7_prod_snapshot_after.json") -Encoding UTF8

Write-Log "Phase 7 production build complete"
