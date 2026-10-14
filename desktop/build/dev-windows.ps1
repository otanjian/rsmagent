# Launch the desktop app in development mode on Windows.
#
# Two traps this closes, both taken straight from the 2026-10-13 verification
# report:
#
#   * ELECTRON_RUN_AS_NODE inherited from a wrapper shell turns electron.exe
#     into plain Node: the app exits with code 0, prints nothing, and the real
#     error sits on stderr (section 1.1a). The variable is cleared here, in the
#     process that is about to start Electron;
#   * the local backend needs an interpreter that actually runs (section 4.2),
#     so a prepared .venv-win is put at the front of PATH for this process only.
#     The shell still probes whatever it finds (desktop/src/main/python-probe.ts)
#     and reports a specific reason if none of it works.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File desktop\build\dev-windows.ps1
#   powershell -ExecutionPolicy Bypass -File desktop\build\dev-windows.ps1 -NoBuild
#
# Nothing here touches config.json, identity.db, run.log or memory directories.

[CmdletBinding()]
param(
    # Skip `npm run build` and start Electron with the current dist/ output.
    [switch]$NoBuild
)

$ErrorActionPreference = 'Stop'

$buildDir = $PSScriptRoot
$repo = Split-Path -Parent (Split-Path -Parent $buildDir)
$desktopDir = Join-Path $repo 'desktop'
$venvDir = Join-Path $repo '.venv-win'
$venvScripts = Join-Path $venvDir 'Scripts'

function Fail([string]$Message) {
    Write-Host "dev-windows: $Message" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path (Join-Path $desktopDir 'package.json'))) {
    Fail "no desktop/package.json under $repo"
}

# 1. Never let Electron run as Node. This is the documented silent-exit trap.
Remove-Item Env:\ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
if (Test-Path Env:\ELECTRON_RUN_AS_NODE) {
    Fail "ELECTRON_RUN_AS_NODE is still set; Electron would start as Node and exit silently"
}

# 2. Put the prepared environment first, so the backend probe finds it before
#    the Microsoft Store placeholder. A venv that exists but does not answer is
#    reported here, not left to look like a mysterious startup failure.
if (Test-Path (Join-Path $venvScripts 'python.exe')) {
    $venvPython = Join-Path $venvScripts 'python.exe'
    $answer = & $venvPython -c "print('ok')" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Fail "$venvPython exists but cannot run: $($answer | Out-String). Rerun desktop\build\setup-windows.ps1"
    }
    $env:PATH = "$venvScripts;$env:PATH"
    Write-Host "dev-windows: using $venvPython"
}
else {
    Write-Host "dev-windows: no .venv-win -- run desktop\build\setup-windows.ps1 to create one" -ForegroundColor Yellow
}

# 3. Build (renderer + main) unless asked not to, then hand over to Electron.
Push-Location $desktopDir
try {
    if (-not $NoBuild) {
        Write-Host "dev-windows: npm run build"
        npm run build
        if ($LASTEXITCODE -ne 0) { Fail "npm run build failed (exit code $LASTEXITCODE)" }
    }

    $electron = Join-Path $desktopDir 'node_modules\electron\dist\electron.exe'
    if (-not (Test-Path $electron)) {
        $electron = Join-Path $desktopDir 'node_modules\.bin\electron.cmd'
    }
    if (-not (Test-Path $electron)) {
        Fail "Electron is not installed under desktop\node_modules -- run npm install in desktop\ first"
    }

    Write-Host "dev-windows: starting $electron"
    & $electron .
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
