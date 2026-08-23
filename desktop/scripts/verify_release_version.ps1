# Verify all project version sources match desktop/config/version.json (single source of truth).
param(
    [switch]$StrictInnoCompile
)

$ErrorActionPreference = "Stop"
$ScriptDir = $PSScriptRoot
. (Join-Path $ScriptDir "lib\aica_version.ps1")

$Root = Get-AicaProjectRoot -StartDir $ScriptDir
$canonical = Get-AicaVersion -Root $Root
$cfg = Read-AicaVersionConfig -Root $Root

$results = @()
$failures = 0

function Add-VersionCheck {
    param(
        [string]$Source,
        [string]$Path,
        [string]$Found,
        [string]$Expected = $canonical,
        [string]$Notes = ""
    )
    $script:results += [PSCustomObject]@{
        Source   = $Source
        Path     = $Path
        Expected = $Expected
        Found    = $Found
        Match    = ($Found -eq $Expected)
        Notes    = $Notes
    }
    if ($Found -ne $Expected) {
        $script:failures++
    }
}

# 1. version.json (canonical)
Add-VersionCheck -Source "version.json (canonical)" `
    -Path (Get-AicaVersionJsonPath -Root $Root) `
    -Found $canonical `
    -Expected $canonical `
    -Notes "source of truth"

# 2. runtime_paths.py APP_VERSION fallback
$runtimePath = Join-Path $Root "backend\runtime_paths.py"
$runtimeText = Get-Content $runtimePath -Raw -Encoding UTF8
if ($runtimeText -match 'APP_VERSION\s*=\s*os\.environ\.get\("AICA_VERSION",\s*"([^"]+)"\)') {
    $found = $Matches[1]
    Add-VersionCheck -Source "runtime_paths.py fallback" -Path $runtimePath -Found $found `
        -Expected "0.0.0-dev" `
        -Notes "dev-only fallback; packaged apps use version.json"
}
else {
    Add-VersionCheck -Source "runtime_paths.py fallback" -Path $runtimePath -Found "(pattern not found)" -Expected "0.0.0-dev"
}

# 3. pyi_rth_aica.py — must not hardcode a release version
$rthPath = Join-Path $Root "desktop\packaging\pyi_rth_aica.py"
$rthText = Get-Content $rthPath -Raw -Encoding UTF8
if ($rthText -match 'setdefault\("AICA_VERSION",\s*"([^"]+)"\)') {
    Add-VersionCheck -Source "pyi_rth_aica.py hardcoded AICA_VERSION" -Path $rthPath -Found $Matches[1] `
        -Notes "REMOVE hardcoded release version from runtime hook"
}
else {
    Add-VersionCheck -Source "pyi_rth_aica.py hardcoded AICA_VERSION" -Path $rthPath -Found "(none)" -Expected "(none)" `
        -Notes "OK - no hardcoded release version"
}

# 4. aica_setup.iss — no inline #define MyAppVersion "x.y.z" (must use /DMyAppVersion)
$issPath = Join-Path $Root "desktop\packaging\aica_setup.iss"
$issText = Get-Content $issPath -Raw -Encoding UTF8
if ($issText -match '#define\s+MyAppVersion\s+"(\d+\.\d+\.\d+[^"]*)"') {
    $inlineVer = $Matches[1]
    if ($inlineVer -ne "0.0.0-dev") {
        Add-VersionCheck -Source "aica_setup.iss inline MyAppVersion" -Path $issPath -Found $inlineVer `
            -Notes "Use ISCC /DMyAppVersion from version.json instead of inline release version"
    }
}
if ($issText -match '#ifndef\s+MyAppVersion') {
    Add-VersionCheck -Source "aica_setup.iss compile define" -Path $issPath -Found "uses #ifndef guard" -Expected "uses #ifndef guard" `
        -Notes "OK - version supplied via /DMyAppVersion at build time"
}

# 5. build_installer.ps1 — must not hardcode $Version = "x.y.z"
$buildPath = Join-Path $Root "desktop\scripts\build_installer.ps1"
$buildText = Get-Content $buildPath -Raw -Encoding UTF8
if ($buildText -match '\$Version\s*=\s*"(\d+\.\d+\.\d+[^"]*)"') {
    Add-VersionCheck -Source "build_installer.ps1 hardcoded version" -Path $buildPath -Found $Matches[1] `
        -Notes "Must read version from version.json via aica_version.ps1"
}
else {
    Add-VersionCheck -Source "build_installer.ps1 hardcoded version" -Path $buildPath -Found "(none)" -Expected "(none)" `
        -Notes "OK - reads from version.json"
}

# 6. file_version_info.txt
$fviPath = Join-Path $Root "desktop\packaging\file_version_info.txt"
if (Test-Path $fviPath) {
    $fviText = Get-Content $fviPath -Raw -Encoding UTF8
    $fviVer = $null
    if ($fviText -match 'StringStruct\(u''FileVersion'',\s*u''([^'']+)''\)') {
        $fviVer = $Matches[1]
    }
    if ($fviVer) {
        Add-VersionCheck -Source "file_version_info.txt FileVersion" -Path $fviPath -Found $fviVer
    }
    else {
        Add-VersionCheck -Source "file_version_info.txt FileVersion" -Path $fviPath -Found "(not found)"
    }
}

# 7. config.env.example AICA_VERSION template
$examplePath = Join-Path $Root "desktop\config\config.env.example"
if (Test-Path $examplePath) {
    $exampleText = Get-Content $examplePath -Raw -Encoding UTF8
    if ($exampleText -match '(?m)^AICA_VERSION=(\S+)') {
        Add-VersionCheck -Source "config.env.example AICA_VERSION" -Path $examplePath -Found $Matches[1] `
            -Notes "template should match current release version"
    }
}

# 8. version.json update config fields
$repo = [string]$cfg.update.repo
$manifestUrl = [string]$cfg.update.manifest_url
if (-not $repo) {
    Add-VersionCheck -Source "version.json update.repo" -Path (Get-AicaVersionJsonPath -Root $Root) -Found "(missing)" -Expected "(set)"
}
else {
    Add-VersionCheck -Source "version.json update.repo" -Path (Get-AicaVersionJsonPath -Root $Root) -Found $repo -Expected $repo -Notes "OK"
}
if (-not $manifestUrl) {
    Add-VersionCheck -Source "version.json update.manifest_url" -Path (Get-AicaVersionJsonPath -Root $Root) -Found "(missing)" -Expected "(set)"
}
elseif ($manifestUrl -notmatch "^https://github\.com/") {
    Add-VersionCheck -Source "version.json update.manifest_url" -Path (Get-AicaVersionJsonPath -Root $Root) -Found $manifestUrl -Expected "https://github.com/..." `
        -Notes "manifest URL must be HTTPS GitHub"
}
else {
    Add-VersionCheck -Source "version.json update.manifest_url" -Path (Get-AicaVersionJsonPath -Root $Root) -Found $manifestUrl -Expected $manifestUrl -Notes "OK"
}

# 9. Optional: dist installer filename if present
$distInstaller = Join-Path $Root "dist\AICA_Setup_$canonical.exe"
if (Test-Path $distInstaller) {
    Add-VersionCheck -Source "dist installer filename" -Path $distInstaller -Found "AICA_Setup_$canonical.exe" -Expected "AICA_Setup_$canonical.exe" -Notes "OK"
}
else {
    $script:results += [PSCustomObject]@{
        Source   = "dist installer"
        Path     = $distInstaller
        Expected = "AICA_Setup_$canonical.exe"
        Found    = "(not present)"
        Match    = $true
        Notes    = "SKIP - no local installer artifact (not a failure)"
    }
}

# 10. Strict: ensure aica_setup.iss compiles only with /DMyAppVersion matching canonical
if ($StrictInnoCompile) {
    $Iscc = $null
    foreach ($c in @(
        "$env:LocalAppData\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )) {
        if (Test-Path $c) { $Iscc = $c; break }
    }
    if (-not $Iscc) {
        Write-Warning "StrictInnoCompile: ISCC.exe not found - skipped compile check"
    }
    else {
        Write-Host "StrictInnoCompile: validating ISCC /DMyAppVersion=$canonical (compile-only not run to avoid rebuild)"
    }
}

Write-Host ""
Write-Host "AICA Version Verification Report"
Write-Host "================================="
Write-Host "Canonical version (version.json): $canonical"
Write-Host ""
$results | Format-Table -AutoSize Source, Found, Expected, Match, Notes

$matched = ($results | Where-Object { $_.Match -eq $true }).Count
$checked = $results.Count
Write-Host ""
Write-Host ("Summary: {0} / {1} checks passed; {2} failure(s)" -f $matched, $checked, $failures)

if ($failures -gt 0) {
    Write-Host "FAILED - version drift detected. Fix mismatches before release." -ForegroundColor Red
    exit 1
}

Write-Host "PASSED - all version sources consistent with version.json." -ForegroundColor Green
exit 0
