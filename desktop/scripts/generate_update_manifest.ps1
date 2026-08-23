# Generate aica-update-manifest.json for GitHub Releases from real build artifacts.
# Does not fabricate SHA-256, size, or URLs — all values come from inputs or version.json.
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,

    [string]$OutputPath = "",

    [string]$DownloadUrl = "",

    [string]$ReleaseTag = "",

    [string]$PublishedAt = "",

    [string]$ReleaseNotes = "",

    [string]$MinimumSupportedVersion = "",

    [ValidateSet("stable", "beta", "prerelease")]
    [string]$Channel = "stable",

    [switch]$Mandatory
)

$ErrorActionPreference = "Stop"
$ScriptDir = $PSScriptRoot
. (Join-Path $ScriptDir "lib\aica_version.ps1")

$Root = Get-AicaProjectRoot -StartDir $ScriptDir
$cfg = Read-AicaVersionConfig -Root $Root
$version = Get-AicaVersion -Root $Root

$installer = Resolve-Path $InstallerPath -ErrorAction Stop
if (-not (Test-Path $installer)) {
    throw "Installer not found: $InstallerPath"
}

$expectedName = Get-AicaInstallerFilename -Version $version
$actualName = Split-Path $installer -Leaf
if ($actualName -ne $expectedName) {
    throw "Installer filename mismatch. Expected '$expectedName' for version $version, got '$actualName'."
}

# SHA-256 from actual file bytes
$hashObj = Get-FileHash -Path $installer -Algorithm SHA256
$sha256 = $hashObj.Hash.ToLowerInvariant()
$sizeBytes = (Get-Item $installer).Length
if ($sizeBytes -le 0) {
    throw "Installer file size is invalid: $sizeBytes bytes"
}

# Download URL — must be HTTPS and explicitly provided or derived from official repo + tag
$url = $DownloadUrl.Trim()
if (-not $url -and $ReleaseTag) {
    $repo = [string]$cfg.update.repo
    if (-not $repo) {
        throw "version.json update.repo is required when using -ReleaseTag without -DownloadUrl"
    }
    $tag = $ReleaseTag.Trim()
    if ($tag -notmatch '^v') {
        $tag = "v$version"
    }
    $url = "https://github.com/$repo/releases/download/$tag/$expectedName"
}

if (-not $url) {
    throw @"
Download URL is required. Provide either:
  -DownloadUrl 'https://github.com/.../releases/download/vX.Y.Z/AICA_Setup_X.Y.Z.exe'
  -ReleaseTag 'vX.Y.Z'  (constructs URL from version.json update.repo)
"@
}

if ($url -notmatch '^https://') {
    throw "Download URL must use HTTPS: $url"
}

$allowedHosts = @(
    'github.com',
    'objects.githubusercontent.com'
)
try {
    $uri = [Uri]$url
    $hostName = $uri.Host.ToLowerInvariant()
    if ($allowedHosts -notcontains $hostName) {
        throw "Download URL host not allowlisted ($hostName). Allowed: $($allowedHosts -join ', ')"
    }
}
catch {
    throw "Invalid download URL: $url - $($_.Exception.Message)"
}

if (-not $PublishedAt) {
    # Use round-trip ISO-8601 (UTC). Avoid HH:mm:ss — locale time separator can become '.' and break validation.
    $PublishedAt = [DateTime]::UtcNow.ToString("o")
}

if (-not $ReleaseNotes) {
    throw "ReleaseNotes is required (-ReleaseNotes '...'). Do not publish empty release notes."
}

$minSupported = $null
if ($MinimumSupportedVersion) {
    if (-not (Test-AicaSemVer -Version $MinimumSupportedVersion)) {
        throw "Invalid minimum_supported_version: $MinimumSupportedVersion"
    }
    $minSupported = $MinimumSupportedVersion
}

$manifest = [ordered]@{
    version                     = $version
    published_at                = $PublishedAt
    channel                     = $Channel
    minimum_supported_version   = $minSupported
    mandatory                   = [bool]$Mandatory
    release_notes               = $ReleaseNotes
    installer                   = [ordered]@{
        filename   = $expectedName
        url        = $url
        sha256     = $sha256
        size_bytes = [int]$sizeBytes
    }
}

if (-not $OutputPath) {
    $OutputPath = Join-Path $Root "dist\aica-update-manifest.json"
}

if (-not [System.IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $Root $OutputPath
}
$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)
$outDir = Split-Path $OutputPath -Parent
if ($outDir -and -not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
}

$json = $manifest | ConvertTo-Json -Depth 6
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($OutputPath, $json, $utf8NoBom)

Write-Host "OK manifest -> $OutputPath"
Write-Host "  version:    $version"
Write-Host "  filename:   $expectedName"
Write-Host "  sha256:     $sha256"
Write-Host "  size_bytes: $sizeBytes"
Write-Host "  url:        $url"

return $OutputPath
