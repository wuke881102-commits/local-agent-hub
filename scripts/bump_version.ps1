<#
.SYNOPSIS
  Bump the app version in every canonical spot, in one shot.

.DESCRIPTION
  The version string lives in 5 places across 3 build systems. This script keeps
  them in sync so a release is a single command:

      scripts\bump_version.ps1 2.0      (两段式 X.Y：小版本 2.1/2.2…，大版本 3.0/4.0…)

  Spots updated:
    1. frontend\src\version.ts        -> export const APP_VERSION = '<v>'   (sidebar badge)
    2. frontend\package.json          -> "version": "<v>"                   (npm)
    3. build\installer.iss            -> #define MyAppVersion "<v>"         (installer + filename)
    4. backend\app\main.py            -> APP_VERSION = "<v>"                (FastAPI + /api/health)
    5. backend\version_info.txt       -> filevers/prodvers + File/ProductVersion (exe 版本资源)

  After bumping, run build\build_installer.cmd to produce LocalAgentHub-Setup-<v>.exe.
#>
param(
  [Parameter(Mandatory = $true, Position = 0)]
  [string] $Version
)

$ErrorActionPreference = 'Stop'

# 版本方案：两段式 X.Y。小版本 2.1 / 2.2…，大版本 3.0 / 4.0…
if ($Version -notmatch '^\d+\.\d+$') {
  Write-Error "Version must be X.Y (e.g. 2.0, 2.1, 3.0). Got: '$Version'"
  exit 1
}

# package.json 的 version 必须是合法 semver（否则 npm install 会报 Invalid Version），
# 这里镜像成三段式 X.Y.0；其余 3 处（侧边栏徽章 / 安装包 / /api/health）都用展示版本 X.Y。
$NpmVersion = "$Version.0"

# Windows 版本资源要四段式整数元组与 X.Y.0.0 字符串（backend\version_info.txt）。
$Parts = $Version.Split('.')
$Major = $Parts[0]
$Minor = $Parts[1]
$VerFile = "$Version.0.0"

# Repo root = parent of the scripts\ folder.
$root = Split-Path -Parent $PSScriptRoot

# Each edit: file path, regex (first match only), replacement, and whether to keep a UTF-8 BOM.
$edits = @(
  @{ Path = 'frontend\src\version.ts'; Pattern = "export const APP_VERSION = '[^']*';"; Replace = "export const APP_VERSION = '$Version';"; Bom = $false },
  @{ Path = 'frontend\package.json';   Pattern = '"version":\s*"[^"]*"';                Replace = "`"version`": `"$NpmVersion`"";       Bom = $false },
  @{ Path = 'build\installer.iss';     Pattern = '#define MyAppVersion "[^"]*"';         Replace = "#define MyAppVersion `"$Version`""; Bom = $true  },
  @{ Path = 'backend\app\main.py';     Pattern = 'APP_VERSION = "[^"]*"';                Replace = "APP_VERSION = `"$Version`"";        Bom = $false },
  @{ Path = 'backend\version_info.txt'; Pattern = 'filevers=\(\d+, \d+, \d+, \d+\)';       Replace = "filevers=($Major, $Minor, 0, 0)";  Bom = $false },
  @{ Path = 'backend\version_info.txt'; Pattern = 'prodvers=\(\d+, \d+, \d+, \d+\)';       Replace = "prodvers=($Major, $Minor, 0, 0)";  Bom = $false },
  @{ Path = 'backend\version_info.txt'; Pattern = "StringStruct\('FileVersion', '[^']*'\)";   Replace = "StringStruct('FileVersion', '$VerFile')";    Bom = $false },
  @{ Path = 'backend\version_info.txt'; Pattern = "StringStruct\('ProductVersion', '[^']*'\)"; Replace = "StringStruct('ProductVersion', '$VerFile')"; Bom = $false }
)

$failed = $false
foreach ($e in $edits) {
  $full = Join-Path $root $e.Path
  if (-not (Test-Path $full)) {
    Write-Host "  MISSING  $($e.Path)" -ForegroundColor Red
    $failed = $true
    continue
  }
  # Read as UTF-8 (handles optional BOM transparently).
  $text = [System.IO.File]::ReadAllText($full, [System.Text.UTF8Encoding]::new($false))
  $re = [regex]$e.Pattern
  $m = $re.Match($text)
  if (-not $m.Success) {
    Write-Host "  NO MATCH $($e.Path)  (pattern: $($e.Pattern))" -ForegroundColor Red
    $failed = $true
    continue
  }
  $old = $m.Value
  # Replace ONLY the first occurrence; $e.Replace is a literal (no $-group refs).
  $new = $re.Replace($text, [System.Text.RegularExpressions.MatchEvaluator] { param($x) $e.Replace }, 1)
  [System.IO.File]::WriteAllText($full, $new, [System.Text.UTF8Encoding]::new([bool]$e.Bom))
  Write-Host ("  OK       {0,-32} {1}  ->  {2}" -f $e.Path, $old, $e.Replace) -ForegroundColor Green
}

if ($failed) {
  Write-Error "One or more files could not be updated. Review the output above."
  exit 1
}

Write-Host ""
Write-Host "Version bumped to $Version. Next: run build\build_installer.cmd" -ForegroundColor Cyan
