<#
.SYNOPSIS
  Collect the facts the real-machine acceptance runbook asks for, into one directory.

.DESCRIPTION
  Companion to docs/desktop-real-machine-acceptance-runbook.md, section 8. It writes
  only inside -Out, never deletes anything, and refuses a target that is a drive root,
  the repository root, or the user's home directory.

  Files written:
    versions.txt     branch/HEAD, installer artifacts, interpreter versions, OS, proxy config hash
    processes.txt    python processes and the ports this app uses, with owning PIDs
    listeners.txt    every listening TCP port on the machine (for the "who owns 9899/9876" question)
    probe.txt        GET <Origin>/api/desktop/meta with response headers (when -Origin is given)
    inspection.*     the CDP inspection report and screenshots (when -Cdp is given)
    README.txt       what each file is, and the rule that unexecuted items must not be ticked

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\collect-real-machine-evidence.ps1 `
     -Out openspec\changes\harden-windows-desktop-reliability\evidence\real-machine\2026-10-14 `
     -Origin https://agent.example.com -Cdp http://127.0.0.1:9222
#>
param(
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$Origin = '',
    [string]$Cdp = '',
    [int]$Deadline = 20
)

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot

if (-not [System.IO.Path]::IsPathRooted($Out)) { $Out = Join-Path (Get-Location) $Out }
$Out = [System.IO.Path]::GetFullPath($Out)
$repoFull = [System.IO.Path]::GetFullPath($repo)

# Refuse the targets that would turn a bad argument into a destructive one.
$dangerous = @(
    [System.IO.Path]::GetPathRoot($Out).TrimEnd('\'),
    $repoFull.TrimEnd('\'),
    $HOME.TrimEnd('\')
) | Where-Object { $_ }
if ($dangerous -contains $Out.TrimEnd('\')) {
    Write-Error "refusing -Out '$Out': it is a drive root, the repository root, or the home directory"
    exit 2
}
if ($Out.Length -lt ([System.IO.Path]::GetPathRoot($Out).Length + 4)) {
    Write-Error "refusing -Out '$Out': path is too shallow to be an evidence directory"
    exit 2
}

New-Item -ItemType Directory -Force -Path $Out | Out-Null
Write-Host "collecting into $Out"
Write-Host "  (nothing outside this directory is written, and nothing is ever deleted)"

function Save($name, $lines) {
    $path = Join-Path $Out $name
    $lines | Set-Content -Path $path -Encoding UTF8
    Write-Host ("  wrote {0} ({1} lines)" -f $name, ($lines | Measure-Object).Count)
}

# --- versions ---------------------------------------------------------------
$versionLines = @()
$versionLines += "collected    : " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ssK')
$versionLines += "host         : " + $env:COMPUTERNAME + " / " + [System.Environment]::OSVersion.VersionString
$versionLines += "powershell   : " + $PSVersionTable.PSVersion.ToString()
$versionLines += "git branch   : " + (& git -C $repo rev-parse --abbrev-ref HEAD 2>$null)
$versionLines += "git HEAD     : " + (& git -C $repo rev-parse HEAD 2>$null)
$versionLines += "git status   : " + ((& git -C $repo status --porcelain 2>$null | Measure-Object).Count) + " changed/untracked entries"
foreach ($exe in @('python', 'python3', 'node')) {
    $cmd = Get-Command $exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $versionLines += ("{0,-12}: {1}" -f $exe, $cmd.Source)
        $versionLines += ("{0,-12}: {1}" -f ($exe + ' --version'), ((& $cmd.Source --version 2>&1 | Select-Object -First 1)))
    } else {
        $versionLines += ("{0,-12}: not on PATH" -f $exe)
    }
}
$pkg = Join-Path $repo 'desktop\package.json'
if (Test-Path $pkg) {
    $json = Get-Content $pkg -Raw | ConvertFrom-Json
    $versionLines += "client version: " + $json.version + " (" + $json.build.productName + ")"
}
$release = Join-Path $repo 'desktop\release'
if (Test-Path $release) {
    foreach ($file in (Get-ChildItem $release -Filter '*.exe' -ErrorAction SilentlyContinue)) {
        $versionLines += ("installer    : {0} ({1:N1} MB, {2})" -f $file.Name, ($file.Length / 1MB), $file.LastWriteTime)
    }
} else {
    $versionLines += "installer    : none built (desktop\release does not exist)"
}
$proxy = Join-Path $repo 'docker\nginx.conf.example'
if (Test-Path $proxy) {
    $versionLines += "proxy config : " + (Get-FileHash $proxy -Algorithm SHA256).Hash
}
Save 'versions.txt' $versionLines

# --- processes and listeners ------------------------------------------------
$processLines = @('python processes (client must not start one in remote mode):')
$pythons = Get-Process -Name 'python', 'pythonw', 'python3' -ErrorAction SilentlyContinue
if ($pythons) {
    foreach ($p in $pythons) { $processLines += ("  pid={0} name={1} path={2}" -f $p.Id, $p.ProcessName, $p.Path) }
} else {
    $processLines += '  none'
}
$processLines += ''
$processLines += 'ports this application uses:'
foreach ($port in @(9899, 9876, 9877, 9222)) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        foreach ($c in $conn) { $processLines += ("  {0} <- pid {1}" -f $port, $c.OwningProcess) }
    } else {
        $processLines += ("  {0} : not listening" -f $port)
    }
}
Save 'processes.txt' $processLines

$listeners = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Select-Object LocalAddress, LocalPort, OwningProcess |
    Sort-Object LocalPort |
    ForEach-Object { "  {0}:{1} pid={2}" -f $_.LocalAddress, $_.LocalPort, $_.OwningProcess }
Save 'listeners.txt' @('listening TCP sockets:', $listeners)

# --- metadata probe ---------------------------------------------------------
if ($Origin) {
    $probeLines = @("GET $Origin/api/desktop/meta", "deadline: $Deadline s")
    try {
        $response = Invoke-WebRequest -Uri ("{0}/api/desktop/meta" -f $Origin.TrimEnd('/')) `
            -TimeoutSec $Deadline -UseBasicParsing -ErrorAction Stop
        $probeLines += "status       : " + [int]$response.StatusCode
        foreach ($key in $response.Headers.Keys) {
            $probeLines += ("header       : {0}: {1}" -f $key, $response.Headers[$key])
        }
        $probeLines += "body         : " + $response.Content
    } catch {
        $probeLines += "probe failed : " + $_.Exception.Message
        $probeLines += "(a failed probe is a recorded observation, not a crash: see the runbook)"
    }
    Save 'probe.txt' $probeLines
} else {
    Write-Host '  (no -Origin given: skipping the metadata probe)'
}

# --- CDP inspection ---------------------------------------------------------
if ($Cdp) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    $inspector = Join-Path $repo 'scripts\inspect-console-cdp.py'
    if ($python -and (Test-Path $inspector)) {
        Write-Host "  running the CDP inspection against $Cdp"
        & $python $inspector --endpoint $Cdp --out $Out --deadline $Deadline
    } else {
        Write-Host '  (no python or no inspector: skipping the CDP inspection)'
    }
} else {
    Write-Host '  (no -Cdp given: skipping the CDP inspection)'
}

Save 'README.txt' @(
    'Real-machine acceptance evidence',
    '',
    'versions.txt    branch/HEAD, interpreter versions, installer artifacts, proxy config hash',
    'processes.txt   python processes and this application''s ports, with owning PIDs',
    'listeners.txt   every listening TCP socket (who owns 9899 / 9876 / 9877)',
    'probe.txt       GET <origin>/api/desktop/meta with headers (when -Origin was given)',
    'inspection.json CDP inspection report; a timeout there is an inspection limitation, never a',
    '                product defect (see scripts/inspect-console-cdp.py)',
    'screenshots/    page captures from the same run',
    '',
    'Rule: an item that was not executed must not be ticked in tasks.md. Record the',
    'environment, the operation, the expectation, what actually happened, and this directory.',
    'Steps: docs/desktop-real-machine-acceptance-runbook.md'
)

Write-Host 'done'
