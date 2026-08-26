# Verify release installer + aica-update-manifest.json are consistent.
# Used by build_installer.ps1 so a production build cannot forget the manifest.
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,

    [Parameter(Mandatory = $true)]
    [string]$ManifestPath,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedVersion
)

$ErrorActionPreference = "Stop"
$ScriptDir = $PSScriptRoot
. (Join-Path $ScriptDir "lib\aica_version.ps1")

$installer = Resolve-Path $InstallerPath -ErrorAction Stop
$manifestFile = Resolve-Path $ManifestPath -ErrorAction Stop

if (-not (Test-AicaSemVer -Version $ExpectedVersion)) {
    throw "Invalid ExpectedVersion: $ExpectedVersion"
}

$expectedName = Get-AicaInstallerFilename -Version $ExpectedVersion
$actualName = Split-Path $installer -Leaf
if ($actualName -ne $expectedName) {
    throw "Installer filename mismatch. Expected '$expectedName', got '$actualName'."
}

$hashObj = Get-FileHash -Path $installer -Algorithm SHA256
$sha256 = $hashObj.Hash.ToLowerInvariant()
$sizeBytes = (Get-Item $installer).Length

$manifest = Get-Content $manifestFile -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $manifest) { throw "Manifest is empty or invalid JSON: $manifestFile" }

$failures = @()
if ([string]$manifest.version -ne $ExpectedVersion) {
    $failures += "manifest.version='$($manifest.version)' != expected '$ExpectedVersion'"
}
if ([string]$manifest.channel -ne "stable") {
    $failures += "manifest.channel='$($manifest.channel)' (production builds must be stable)"
}
if (-not $manifest.release_notes) {
    $failures += "manifest.release_notes is empty"
}
if (-not $manifest.published_at) {
    $failures += "manifest.published_at is missing"
}
if (-not $manifest.installer) {
    $failures += "manifest.installer is missing"
}
else {
    if ([string]$manifest.installer.filename -ne $expectedName) {
        $failures += "installer.filename='$($manifest.installer.filename)' != '$expectedName'"
    }
    if ([string]$manifest.installer.sha256 -ne $sha256) {
        $failures += "installer.sha256 mismatch vs actual file"
    }
    if ([int64]$manifest.installer.size_bytes -ne [int64]$sizeBytes) {
        $failures += "installer.size_bytes=$($manifest.installer.size_bytes) != actual $sizeBytes"
    }
    $url = [string]$manifest.installer.url
    if ($url -notmatch '^https://') {
        $failures += "installer.url must be https"
    }
    if ($url -notmatch [regex]::Escape($expectedName)) {
        $failures += "installer.url does not contain expected filename '$expectedName'"
    }
}

if ($failures.Count -gt 0) {
    Write-Host "RELEASE ARTIFACT VALIDATION FAILED:"
    $failures | ForEach-Object { Write-Host "  - $_" }
    throw "verify_release_artifacts.ps1 failed with $($failures.Count) error(s)."
}

Write-Host "OK release artifacts verified"
Write-Host "  version:  $ExpectedVersion"
Write-Host "  filename: $expectedName"
Write-Host "  sha256:   $sha256"
Write-Host "  size:     $sizeBytes"
Write-Host "  manifest: $manifestFile"
Write-Host "  IMPORTANT: Upload BOTH the installer and aica-update-manifest.json to GitHub Releases."
