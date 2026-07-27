<#
.SYNOPSIS
  Build the Disc2Jelly Windows installer end to end.

.DESCRIPTION
  Runs on Windows only (PyInstaller cannot cross-compile from Linux).
  Steps: install build deps, fetch HandBrakeCLI, generate baked defaults,
  run PyInstaller, run Inno Setup. Output lands in dist\.

.PARAMETER BakePassword
  Compile the WebDAV password from build_config.toml into the binary.
  Off by default and it should stay off: PyInstaller does not obfuscate, so
  `strings Disc2Jelly.exe` recovers it. The installer wizard asks for the
  password on the target machine instead.

.PARAMETER AllowUnpinned
  Allow HandBrakeCLI to be downloaded without a pinned SHA-256. Use once, to
  discover the digest; never for a build you hand to someone.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File build\build_windows.ps1
#>
[CmdletBinding()]
param(
    [switch]$BakePassword,
    [switch]$AllowUnpinned
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

Write-Host "== Disc2Jelly Windows build ==" -ForegroundColor Cyan

# 1. Build dependencies -------------------------------------------------------
Write-Host "`n[1/5] Installing build dependencies..." -ForegroundColor Cyan
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller

# 2. Third-party binaries -----------------------------------------------------
Write-Host "`n[2/5] Fetching HandBrakeCLI..." -ForegroundColor Cyan
$fetchArgs = @("build\fetch_deps.py")
if ($AllowUnpinned) { $fetchArgs += "--allow-unpinned" }
python @fetchArgs

# 3. Baked defaults -----------------------------------------------------------
Write-Host "`n[3/5] Generating baked defaults..." -ForegroundColor Cyan
$genArgs = @("build\gen_baked.py")
if ($BakePassword) {
    Write-Warning "Baking a WebDAV password into the binary. It is recoverable with 'strings'. Do not publish this installer."
    $genArgs += "--bake-password"
}
python @genArgs

# 4. PyInstaller --------------------------------------------------------------
Write-Host "`n[4/5] Running PyInstaller..." -ForegroundColor Cyan
if (Test-Path "dist\Disc2Jelly") { Remove-Item -Recurse -Force "dist\Disc2Jelly" }
pyinstaller build\disc2jelly.spec --noconfirm --distpath dist --workpath build\pyi

# 5. Inno Setup ---------------------------------------------------------------
Write-Host "`n[5/5] Building the installer..." -ForegroundColor Cyan
$iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    )
    $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) {
    throw "Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php"
}

# Pass the non-secret defaults through so the wizard fields arrive pre-filled.
$defs = @()
if (Test-Path "build_config.toml") {
    $cfg = python -c @"
import tomllib, json
print(json.dumps(tomllib.load(open('build_config.toml','rb'))))
"@ | ConvertFrom-Json
    if ($cfg.webdav_url)  { $defs += "/DDefaultWebdavUrl=$($cfg.webdav_url)" }
    if ($cfg.webdav_user) { $defs += "/DDefaultWebdavUser=$($cfg.webdav_user)" }
    if ($cfg.local_path)  { $defs += "/DDefaultLocalPath=$($cfg.local_path)" }
}

& $iscc @defs "build\disc2jelly.iss"

Write-Host "`nDone. Installer is in dist\" -ForegroundColor Green
Write-Host "Note: the installer is unsigned, so SmartScreen will warn once." -ForegroundColor Yellow
Write-Host "      Click 'More info' then 'Run anyway'." -ForegroundColor Yellow
