# Shared AICA version helpers — single source of truth: desktop/config/version.json
$ErrorActionPreference = "Stop"

function Get-AicaProjectRoot {
    param([string]$StartDir = $PSScriptRoot)
    $dir = (Resolve-Path $StartDir).Path
    for ($i = 0; $i -lt 8; $i++) {
        $versionFile = Join-Path $dir "desktop\config\version.json"
        if (Test-Path $versionFile) {
            return $dir
        }
        $parent = Split-Path $dir -Parent
        if (-not $parent -or $parent -eq $dir) {
            break
        }
        $dir = $parent
    }
    throw "Could not locate project root containing desktop/config/version.json (started at $StartDir)"
}

function Get-AicaVersionJsonPath {
    param([string]$Root = (Get-AicaProjectRoot))
    return Join-Path $Root "desktop\config\version.json"
}

function Read-AicaVersionConfig {
    param(
        [string]$Root = (Get-AicaProjectRoot)
    )
    $path = Get-AicaVersionJsonPath -Root $Root
    if (-not (Test-Path $path)) {
        throw "Missing version source of truth: $path"
    }
    try {
        $raw = Get-Content $path -Raw -Encoding UTF8
        return ($raw | ConvertFrom-Json)
    }
    catch {
        throw "Failed to parse $path : $_"
    }
}

function Get-AicaVersion {
    param(
        [string]$Root = (Get-AicaProjectRoot)
    )
    $cfg = Read-AicaVersionConfig -Root $Root
    $version = [string]$cfg.version
    if (-not $version) {
        throw "version.json is missing required field: version"
    }
    if (-not (Test-AicaSemVer -Version $version)) {
        throw "version.json version is not valid semver: $version"
    }
    return $version.Trim()
}

function Test-AicaSemVer {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Version
    )
    return $Version -match '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$'
}

function Get-AicaInstallerFilename {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Version
    )
    return "AICA_Setup_$Version.exe"
}

function Sync-AicaFileVersionInfo {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Version,
        [string]$Root = (Get-AicaProjectRoot)
    )
    if (-not (Test-AicaSemVer -Version $Version)) {
        throw "Invalid semver for file_version_info sync: $Version"
    }
    $parts = $Version.Split('.')
    if ($parts.Count -lt 3) {
        throw "Version must have at least major.minor.patch: $Version"
    }
    $major = [int]$parts[0]
    $minor = [int]$parts[1]
    $patch = [int]$parts[2]

    $utf8NoBom = New-Object System.Text.UTF8Encoding $false

    $dest = Join-Path $Root "desktop\packaging\file_version_info.txt"
    $content = @"
# -*- coding: utf-8 -*-
# Auto-synced from desktop/config/version.json - do not edit manually.
"""PyInstaller Windows version resource for AICA.exe"""
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($major, $minor, $patch, 0),
    prodvers=($major, $minor, $patch, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'040904B0',
        [
          StringStruct(u'CompanyName', u'AICA'),
          StringStruct(u'FileDescription', u'AICA - Financial Intelligence'),
          StringStruct(u'FileVersion', u'$Version'),
          StringStruct(u'InternalName', u'AICA'),
          StringStruct(u'LegalCopyright', u'AICA'),
          StringStruct(u'OriginalFilename', u'AICA.exe'),
          StringStruct(u'ProductName', u'AICA'),
          StringStruct(u'ProductVersion', u'$Version'),
        ]
      )
    ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"@
    [System.IO.File]::WriteAllText($dest, $content, $utf8NoBom)

    $destUpdater = Join-Path $Root "desktop\packaging\file_version_info_updater.txt"
    $contentUpdater = @"
# -*- coding: utf-8 -*-
# Auto-synced from desktop/config/version.json - do not edit manually.
# PyInstaller Windows version resource for AICA.Updater.exe
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($major, $minor, $patch, 0),
    prodvers=($major, $minor, $patch, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'040904B0',
        [
          StringStruct(u'CompanyName', u'AICA'),
          StringStruct(u'FileDescription', u'AICA Updater'),
          StringStruct(u'FileVersion', u'$Version'),
          StringStruct(u'InternalName', u'AICA.Updater'),
          StringStruct(u'LegalCopyright', u'AICA'),
          StringStruct(u'OriginalFilename', u'AICA.Updater.exe'),
          StringStruct(u'ProductName', u'AICA'),
          StringStruct(u'ProductVersion', u'$Version'),
        ]
      )
    ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"@
    [System.IO.File]::WriteAllText($destUpdater, $contentUpdater, $utf8NoBom)
    return $dest
}

function Update-AicaVersionBuildStamp {
    param(
        [string]$Root = (Get-AicaProjectRoot),
        [string]$BuildStamp = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
    )
    $path = Get-AicaVersionJsonPath -Root $Root
    $cfg = Read-AicaVersionConfig -Root $Root
    $cfg.build = $BuildStamp
    # UTF-8 without BOM — BOM breaks Python json.loads(..., encoding="utf-8").
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    $json = ($cfg | ConvertTo-Json -Depth 8)
    if (-not $json.EndsWith("`n")) { $json = $json + "`n" }
    [System.IO.File]::WriteAllText($path, $json, $utf8NoBom)
    return $BuildStamp
}

function Initialize-AicaBuildEnvironment {
    param(
        [string]$Root = (Get-AicaProjectRoot),
        [switch]$UpdateBuildStamp
    )
    $version = Get-AicaVersion -Root $Root
    $build = if ($UpdateBuildStamp) {
        Update-AicaVersionBuildStamp -Root $Root
    }
    else {
        $cfg = Read-AicaVersionConfig -Root $Root
        [string]$cfg.build
    }
    $env:AICA_VERSION = $version
    if ($build) {
        $env:AICA_BUILD = $build
    }
    Sync-AicaFileVersionInfo -Version $version -Root $Root | Out-Null
    return [PSCustomObject]@{
        Version = $version
        Build   = $build
        Root    = $Root
    }
}
