# Prepare a Windows development environment for the desktop app.
#
# Why this exists: a checkout whose .venv was created on macOS has no Scripts/
# directory at all, and PATH's `python` on Windows is the Microsoft Store
# placeholder -- which exists, starts, and refuses to be an interpreter. Both
# produce "The app exited during startup (code 9009)" with nothing pointing at
# the cause (docs/design/windows-desktop-verification-2026-10-13.md section 4.2).
#
# This script resolves a real interpreter, creates or reuses .venv-win, and
# installs desktop/build/requirements-desktop.txt into it. Preparation is
# additive: the macOS .venv, config.json, identity.db, run.log and any memory
# directory are never read, written or deleted by it.
#
# Usage (from anywhere):
#   powershell -ExecutionPolicy Bypass -File desktop\build\setup-windows.ps1
#   powershell -ExecutionPolicy Bypass -File desktop\build\setup-windows.ps1 -Check
#   powershell -ExecutionPolicy Bypass -File desktop\build\setup-windows.ps1 -Python C:\Python312\python.exe
#
# Exit codes: 0 prepared (or -Check found everything in place), 1 a named
# problem (printed), never a silent success.

[CmdletBinding()]
param(
    # Interpreter to build the environment with. Defaults to COW_PYTHON_PATH,
    # then to whatever `python`/`python3` resolves to -- each one is *probed*,
    # not trusted.
    [string]$Python = $env:COW_PYTHON_PATH,
    # Report what would happen and validate the interpreter, creating nothing.
    [switch]$Check
)

$ErrorActionPreference = 'Stop'

$buildDir = $PSScriptRoot
$repo = Split-Path -Parent (Split-Path -Parent $buildDir)
$venvDir = Join-Path $repo '.venv-win'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$requirements = Join-Path $buildDir 'requirements-desktop.txt'

# Must match MIN_PYTHON / MAX_PYTHON_EXCLUSIVE in agent/skills/dependencies.py
# (the bundle's build check enforces the same window).
$minMinor = 8
$maxMinorExclusive = 14

function Fail([string]$Message) {
    Write-Host "setup-windows: $Message" -ForegroundColor Red
    exit 1
}

function Resolve-Interpreter {
    param([string[]]$Candidates)

    $problems = @()
    $previous = $ErrorActionPreference
    foreach ($candidate in $Candidates) {
        if (-not $candidate) { continue }
        try {
            # stderr is merged on purpose: the Store placeholder explains itself
            # there. The preference is relaxed for this one call, because under
            # 'Stop' merging a native command's stderr becomes a terminating
            # error -- and we would then report PowerShell's wrapper instead of
            # the interpreter's own words.
            $ErrorActionPreference = 'Continue'
            $answer = & $candidate -c "import json,sys;print(json.dumps({'v':list(sys.version_info[:3]),'e':sys.executable}))" 2>&1
            $code = $LASTEXITCODE
        }
        catch {
            $problems += "$candidate -> $($_.Exception.Message)"
            continue
        }
        finally {
            $ErrorActionPreference = $previous
        }

        if ($code -ne 0) {
            $text = ($answer | Out-String).Trim()
            if ($text -match 'Microsoft Store|WindowsApps|was not found') {
                $problems += "$candidate -> Microsoft Store placeholder, not an interpreter"
            }
            else {
                $problems += "$candidate -> exit code $code"
            }
            continue
        }

        $line = ($answer | Where-Object { $_ -match '^\{' } | Select-Object -Last 1)
        if (-not $line) { $problems += "$candidate -> no version in the answer"; continue }
        $info = $line | ConvertFrom-Json
        $major = [int]$info.v[0]
        $minor = [int]$info.v[1]
        if ($major -ne 3 -or $minor -lt $minMinor -or $minor -ge $maxMinorExclusive) {
            $problems += "$candidate -> Python $major.$minor is outside 3.$minMinor-3.$($maxMinorExclusive - 1)"
            continue
        }
        return [pscustomobject]@{
            Command = $candidate
            Version = "$($info.v[0]).$($info.v[1]).$($info.v[2])"
            Executable = $info.e
        }
    }
    Fail ("no usable Python interpreter. Tried: " + ($problems -join '; ') +
          ".`nInstall Python 3.$minMinor-3.$($maxMinorExclusive - 1), or pass -Python <path>, " +
          "or set COW_PYTHON_PATH. The Microsoft Store alias cannot be used.")
}

$candidates = @()
if ($Python) { $candidates += $Python } else { $candidates += @('python', 'python3') }
$interpreter = Resolve-Interpreter -Candidates $candidates
Write-Host "setup-windows: interpreter  $($interpreter.Executable) ($($interpreter.Version))"

if (-not (Test-Path $requirements)) {
    Fail "cannot find $requirements -- run this script from the repository checkout"
}

$ready = (Test-Path $venvPython)
if ($ready) {
    Write-Host "setup-windows: environment  $venvDir (reused)"
}
else {
    Write-Host "setup-windows: environment  $venvDir (will be created)"
}
Write-Host "setup-windows: requirements $requirements"

if ($Check) {
    Write-Host "setup-windows: -Check only -- nothing was created or installed."
    exit 0
}

if (-not $ready) {
    Write-Host "setup-windows: creating the virtual environment..."
    & $interpreter.Command -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        Fail "could not create $venvDir (python -m venv returned $LASTEXITCODE)"
    }
}

Write-Host "setup-windows: installing the desktop requirements..."
& $venvPython -m pip install --disable-pip-version-check -r $requirements
if ($LASTEXITCODE -ne 0) {
    Fail "pip install failed (exit code $LASTEXITCODE). Fix the reported package or network problem and rerun."
}

# The interpreter is *probed* by the desktop shell before it is used
# (desktop/src/main/python-probe.ts), so proving it answers here is the same
# check the app will make -- a venv that exists but cannot start is reported now
# rather than as "exited during startup (code 9009)".
$verify = & $venvPython -c "import sys;print(sys.executable)" 2>&1
if ($LASTEXITCODE -ne 0) {
    Fail "$venvPython exists but cannot run: $($verify | Out-String)"
}

Write-Host ""
Write-Host "setup-windows: ready." -ForegroundColor Green
Write-Host "  interpreter: $venvPython"
Write-Host "  next:        powershell -ExecutionPolicy Bypass -File desktop\build\dev-windows.ps1"
exit 0
