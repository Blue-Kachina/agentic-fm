<#
.SYNOPSIS
    Start, stop or check the agentic-fm companion server on Windows.

.DESCRIPTION
    Runs agent/scripts/companion_server.py with a virtual environment's Python, never
    with a python on PATH (which may be the Microsoft Store stub). The Python is, in order:

      1. $env:AGENTIC_FM_PYTHON, if set
      2. the DIS harness .venv (..\..\.venv from the agentic-fm root), when agentic-fm
         is the harness's tools/agentic-fm submodule
      3. .venv in the agentic-fm root
      4. agent\.venv in the agentic-fm root

    That Python needs fm-saxml-converter installed for POST /convert to work.

.EXAMPLE
    .\start_companion.ps1                      # run in this window (Ctrl-C stops it)
    .\start_companion.ps1 -Background          # run hidden; logs in %LOCALAPPDATA%\agentic-fm
    .\start_companion.ps1 -Status              # GET /health
    .\start_companion.ps1 -Stop                # stop a running companion
    .\start_companion.ps1 -InstallLogonTask    # start it at every logon (Task Scheduler)
    .\start_companion.ps1 -RemoveLogonTask     # undo -InstallLogonTask
    .\start_companion.ps1 -- --port 9000       # anything after -- goes to companion_server.py
#>
[CmdletBinding()]
param(
    [switch]$Background,
    [switch]$Stop,
    [switch]$Status,
    [switch]$InstallLogonTask,
    [switch]$RemoveLogonTask,
    [switch]$PrintPython,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CompanionArgs
)

$ErrorActionPreference = 'Stop'

function Exit-WithError([string]$Message) {
    [Console]::Error.WriteLine($Message)
    exit 1
}

$TaskName = 'agentic-fm companion server'
$AfmRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$Server = Join-Path $AfmRoot 'agent\scripts\companion_server.py'
$LogDir = Join-Path $env:LOCALAPPDATA 'agentic-fm'
$LogFile = Join-Path $LogDir 'companion.log'
$ErrLogFile = Join-Path $LogDir 'companion.err.log'

function Find-CompanionPython {
    $candidates = @()
    if ($env:AGENTIC_FM_PYTHON) {
        if (-not (Test-Path -LiteralPath $env:AGENTIC_FM_PYTHON -PathType Leaf)) {
            Exit-WithError "AGENTIC_FM_PYTHON is set to '$($env:AGENTIC_FM_PYTHON)', which does not exist."
        }
        $candidates += $env:AGENTIC_FM_PYTHON
    }
    # A submodule has a .git *file*; only then is ..\..\.venv the DIS harness's venv.
    if (Test-Path -LiteralPath (Join-Path $AfmRoot '.git') -PathType Leaf) {
        $candidates += Join-Path $AfmRoot '..\..\.venv\Scripts\python.exe'
    }
    $candidates += Join-Path $AfmRoot '.venv\Scripts\python.exe'
    $candidates += Join-Path $AfmRoot 'agent\.venv\Scripts\python.exe'

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $resolved = (Resolve-Path -LiteralPath $candidate).Path
            if ($resolved -like '*\WindowsApps\*') {
                Exit-WithError "Refusing $resolved : it is the Microsoft Store python stub, not a virtual environment."
            }
            return $resolved
        }
    }
    Exit-WithError ("No Python virtual environment found for the companion server. Looked for:`n  " +
           ($candidates -join "`n  ") +
           "`nCreate one (py -3.11 -m venv .venv, then .venv\Scripts\python.exe -m pip install " +
           "fm-saxml-converter), or set AGENTIC_FM_PYTHON to a python.exe.")
}

function Get-CompanionPort {
    $config = Join-Path $AfmRoot 'agent\config\companion.json'
    if ($env:COMPANION_PORT) { return [int]$env:COMPANION_PORT }
    if (Test-Path -LiteralPath $config) {
        try {
            $json = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json
            if ($json.companion -and $json.companion.port) { return [int]$json.companion.port }
        } catch {
            Write-Warning "Could not read $config ($($_.Exception.Message)); using port 8765."
        }
    }
    return 8765
}

function Get-CompanionProcesses {
    Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*companion_server.py*' }
}

if ($Status) {
    $url = "http://127.0.0.1:$(Get-CompanionPort)/health"
    try {
        $health = Invoke-RestMethod -Uri $url -TimeoutSec 5
        Write-Output "Companion is running at $url (version $($health.version))."
        exit 0
    } catch {
        Write-Output "Companion is not responding at $url."
        exit 1
    }
}

if ($Stop) {
    $procs = @(Get-CompanionProcesses)
    if ($procs.Count -eq 0) {
        Write-Output 'No companion server is running.'
        exit 0
    }
    foreach ($proc in $procs) {
        Stop-Process -Id $proc.ProcessId -Force
        Write-Output "Stopped companion server (PID $($proc.ProcessId))."
    }
    exit 0
}

if ($RemoveLogonTask) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed the '$TaskName' logon task. A companion that is already running keeps running (use -Stop)."
    } else {
        Write-Output "There is no '$TaskName' logon task."
    }
    exit 0
}

$python = Find-CompanionPython

if ($PrintPython) {
    Write-Output $python
    exit 0
}

$ErrorActionPreference = 'Continue'     # Windows PowerShell turns native stderr into errors
& $python -c 'import fm_saxml' 2>$null
$hasFmSaxml = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = 'Stop'
if (-not $hasFmSaxml) {
    Write-Warning "fm-saxml is not installed for $python; POST /convert jobs will fail until it is (pip install fm-saxml-converter)."
}

if ($InstallLogonTask) {
    $argument = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" -Background"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argument -WorkingDirectory $AfmRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description 'Starts the agentic-fm companion server at logon.' -Force | Out-Null
    Write-Output "Installed the '$TaskName' logon task: the companion starts at your next logon."
    Write-Output "Start it now with -Background; remove the task with -RemoveLogonTask."
    exit 0
}

$env:PYTHONUTF8 = '1'

if ($Background) {
    if (@(Get-CompanionProcesses).Count -gt 0) {
        Write-Output 'A companion server is already running (see -Status, -Stop).'
        exit 0
    }
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $argList = @("`"$Server`"")
    if ($CompanionArgs) { $argList += @($CompanionArgs | ForEach-Object { "`"$_`"" }) }
    $proc = Start-Process -FilePath $python -ArgumentList $argList -WorkingDirectory $AfmRoot `
        -WindowStyle Hidden -RedirectStandardOutput $LogFile -RedirectStandardError $ErrLogFile -PassThru
    Write-Output "Started the companion server (PID $($proc.Id)) with $python"
    Write-Output "Logs: $LogFile"
    exit 0
}

Write-Output "Starting the companion server with $python (Ctrl-C stops it)"
Set-Location -LiteralPath $AfmRoot
& $python $Server @CompanionArgs
exit $LASTEXITCODE
