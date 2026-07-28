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

.PARAMETER HandBrakeArchive
  Path to an already-downloaded HandBrakeCLI-<version>-win-x86_64.zip, for
  machines where the download fails (TLS-inspecting proxy, no internet). The
  pinned checksum is still verified.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File build\build_windows.ps1
#>
[CmdletBinding()]
param(
    [switch]$BakePassword,
    [switch]$AllowUnpinned,
    [string]$HandBrakeArchive
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# $ErrorActionPreference does not apply to native commands in Windows
# PowerShell, so a failing python/iscc would otherwise let the build carry on
# and ship a broken installer. Every external call goes through this.
# The command goes in a script block so its arguments are parsed as native
# command arguments (a bare -m would otherwise bind as a parameter name).
function Invoke-Step {
    param([string]$What, [scriptblock]$Body)
    & $Body
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed with exit code $LASTEXITCODE. Build aborted."
    }
}

Write-Host "== Disc2Jelly Windows build ==" -ForegroundColor Cyan

# 1. Build dependencies -------------------------------------------------------
Write-Host "`n[1/5] Installing build dependencies..." -ForegroundColor Cyan
Invoke-Step "pip install --upgrade pip" { python -m pip install --upgrade pip }
Invoke-Step "pip install -r requirements.txt" { python -m pip install -r requirements.txt }
Invoke-Step "pip install pyinstaller" { python -m pip install pyinstaller certifi }

# 2. Third-party binaries -----------------------------------------------------
Write-Host "`n[2/5] Fetching HandBrakeCLI..." -ForegroundColor Cyan
$fetchArgs = @("build\fetch_deps.py")
if ($AllowUnpinned) { $fetchArgs += "--allow-unpinned" }
if ($HandBrakeArchive) { $fetchArgs += @("--archive", $HandBrakeArchive) }
Invoke-Step "HandBrakeCLI fetch" { python @fetchArgs }

# 3. Baked defaults -----------------------------------------------------------
Write-Host "`n[3/5] Generating baked defaults..." -ForegroundColor Cyan
$genArgs = @("build\gen_baked.py")
if ($BakePassword) {
    Write-Warning "Baking a WebDAV password into the binary. It is recoverable with 'strings'. Do not publish this installer."
    $genArgs += "--bake-password"
}
Invoke-Step "baked defaults" { python @genArgs }

# 4. PyInstaller --------------------------------------------------------------
Write-Host "`n[4/5] Running PyInstaller..." -ForegroundColor Cyan
if (Test-Path "dist\Disc2Jelly") { Remove-Item -Recurse -Force "dist\Disc2Jelly" }
# `python -m PyInstaller`, not the `pyinstaller` shim: pip's Scripts directory
# is frequently not on PATH, especially for Microsoft Store Python.
Invoke-Step "PyInstaller" { python -m PyInstaller build\disc2jelly.spec --noconfirm --distpath dist --workpath build\pyi }

# 5. Inno Setup ---------------------------------------------------------------
Write-Host "`n[5/5] Building the installer..." -ForegroundColor Cyan
$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
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

# Wizard defaults come from a generated include file, not /D switches: the
# WebDAV password can travel this way, and a command line is readable by every
# process on this machine.
if (-not (Test-Path "build_config.toml")) {
    # Silence here used to look like the settings had simply been ignored.
    Write-Warning "No build_config.toml (copy build_config.example.toml). No TMDb key is baked in and the installer wizard asks for everything."
}
Invoke-Step "wizard defaults" { python build\gen_wizard_defaults.py }

Invoke-Step "Inno Setup" { & $iscc "build\disc2jelly.iss" }

Write-Host "`nDone. Installer is in dist\" -ForegroundColor Green
Write-Host "Note: the installer is unsigned, so SmartScreen will warn once." -ForegroundColor Yellow
Write-Host "      Click 'More info' then 'Run anyway'." -ForegroundColor Yellow
