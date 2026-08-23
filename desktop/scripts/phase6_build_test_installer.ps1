# Phase 6: build a LOCAL test installer (1.0.3) without Desktop publish or production deploy.
# Restores desktop/config/version.json to 1.0.2 afterward.
# Does NOT commit, push, tag, or install to %LOCALAPPDATA%\AICA.
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root

. (Join-Path $PSScriptRoot "lib\aica_version.ps1")

$VersionFile = Join-Path $Root "desktop\config\version.json"
$Backup = Join-Path $Root "dist\phase6_version.json.bak"
$Log = Join-Path $Root "dist\phase6_build_installer.log"

function Write-Log($msg) {
    $line = "$(Get-Date -Format o) $msg"
    Add-Content -Path $Log -Value $line
    Write-Host $msg
}

# Snapshot production install for safety checks later
$ProdExe = Join-Path $env:LOCALAPPDATA "AICA\AICA.exe"
$ProdVer = Join-Path $env:LOCALAPPDATA "AICA\version.json"
$snap = @{
    prod_exe_exists = (Test-Path $ProdExe)
    prod_exe_len    = if (Test-Path $ProdExe) { (Get-Item $ProdExe).Length } else { $null }
    prod_exe_mtime  = if (Test-Path $ProdExe) { (Get-Item $ProdExe).LastWriteTimeUtc.ToString("o") } else { $null }
    prod_version    = if (Test-Path $ProdVer) { (Get-Content $ProdVer -Raw) } else { $null }
}
$snap | ConvertTo-Json | Set-Content (Join-Path $Root "dist\phase6_prod_snapshot.json") -Encoding UTF8

Copy-Item -Force $VersionFile $Backup
Write-Log "Backed up version.json -> $Backup"

try {
    # Temporary numeric test version (prerelease compares equal to base triple).
    $cfg = Get-Content $VersionFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $cfg.version = "1.0.3"
    $cfg.build = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK") + "-phase6-test"
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($VersionFile, ($cfg | ConvertTo-Json -Depth 8), $utf8)
    Write-Log "Temporarily set version.json to 1.0.3 (local only)"

    $env:AICA_VERSION = "1.0.3"
    Sync-AicaFileVersionInfo -Version "1.0.3" -Root $Root | Out-Null

    Write-Log "==> build_engine.ps1"
    & (Join-Path $PSScriptRoot "build_engine.ps1")
    Write-Log "==> build_launcher.ps1"
    & (Join-Path $PSScriptRoot "build_launcher.ps1")
    Write-Log "==> build_updater.ps1"
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
    if (-not $Iscc) { throw "Inno Setup 6 (ISCC.exe) not found" }

    $Iss = Join-Path $Root "desktop\packaging\aica_setup.iss"
    Write-Log "==> ISCC /DMyAppVersion=1.0.3 (no Desktop copy)"
    & $Iscc "/DMyAppVersion=1.0.3" $Iss
    $Setup = Join-Path $Root "dist\AICA_Setup_1.0.3.exe"
    if (-not (Test-Path $Setup)) { throw "Missing $Setup" }
    Write-Log "OK test installer -> $Setup size=$((Get-Item $Setup).Length)"
}
finally {
    if (Test-Path $Backup) {
        Copy-Item -Force $Backup $VersionFile
        Write-Log "Restored version.json from backup"
        Sync-AicaFileVersionInfo -Version "1.0.2" -Root $Root | Out-Null
        $env:AICA_VERSION = "1.0.2"
    }
}

Write-Log "Phase 6 installer build finished"
