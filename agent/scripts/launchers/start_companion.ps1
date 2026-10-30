<#
.SYNOPSIS
    Start, stop, watch or check the agentic-fm companion server on Windows.

.DESCRIPTION
    Runs agent/scripts/companion_server.py with a virtual environment's Python, never
    with a python on PATH (which may be the Microsoft Store stub). The Python is, in order:

      1. $env:AGENTIC_FM_PYTHON, if set
      2. the DIS harness .venv (..\..\.venv from the agentic-fm root), when agentic-fm
         is the harness's tools/agentic-fm submodule
      3. .venv in the agentic-fm root
      4. agent\.venv in the agentic-fm root

    That Python needs fm-saxml-converter installed for POST /convert to work.

    -Status, -Attach and -Stop call the companion's own status, attach and stop commands:
    they find the running companion from its state file and GET /health, and -Stop never
    kills a conversion halfway unless you add -- --force. The companion writes its own log,
    %LOCALAPPDATA%\agentic-fm\companion-server.log, however it was started.

.EXAMPLE
    .\start_companion.ps1                      # run in this window (Ctrl-C stops it)
    .\start_companion.ps1 -Background          # run hidden (it still writes its own log)
    .\start_companion.ps1 -Status              # who is running: PID, start time, folder, log file
    .\start_companion.ps1 -Attach              # show and follow its log; Ctrl-C detaches or stops it
    .\start_companion.ps1 -Stop                # stop it cleanly
    .\start_companion.ps1 -Stop -- --force     # stop it even while a conversion is running
    .\start_companion.ps1 -InstallLogonTask    # start it at every logon (Task Scheduler)
    .\start_companion.ps1 -RemoveLogonTask     # undo -InstallLogonTask
    .\start_companion.ps1 -- --port 9000       # anything after -- goes to companion_server.py
#>
[CmdletBinding()]
param(
    [switch]$Background,
    [switch]$Stop,
    [switch]$Status,
    [switch]$Attach,
    [switch]$InstallLogonTask,
    [switch]$RemoveLogonTask,
    [switch]$PrintPython,
    # Who started a -Background companion, shown by -Status and GET /health (the logon task passes "logon task")
    [string]$LaunchedBy = 'background',
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
# The companion's state folder (log file, state file, conversion records); COMPANION_STATE_DIR moves it.
$StateDir = if ($env:COMPANION_STATE_DIR) { $env:COMPANION_STATE_DIR } else { Join-Path $env:LOCALAPPDATA 'agentic-fm' }
# Only Python's own errors before the companion opens its log file land here.
$ErrLogFile = Join-Path $StateDir 'companion.err.log'

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

$env:PYTHONUTF8 = '1'

# Runs one of the companion's own commands (status, attach, stop) in this window and exits with its code.
function Invoke-CompanionCommand([string]$Command) {
    $ErrorActionPreference = 'Continue'     # Windows PowerShell turns native stderr into errors
    Set-Location -LiteralPath $AfmRoot
    & $python $Server $Command @CompanionArgs
    exit $LASTEXITCODE
}

if ($Status) { Invoke-CompanionCommand 'status' }
if ($Attach) { Invoke-CompanionCommand 'attach' }
if ($Stop) { Invoke-CompanionCommand 'stop' }

$ErrorActionPreference = 'Continue'
& $python -c 'import fm_saxml' 2>$null
$hasFmSaxml = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = 'Stop'
if (-not $hasFmSaxml) {
    Write-Warning "fm-saxml is not installed for $python; POST /convert jobs will fail until it is (pip install fm-saxml-converter)."
}

if ($InstallLogonTask) {
    $argument = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" -Background -LaunchedBy `"logon task`""
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

if ($Background) {
    $ErrorActionPreference = 'Continue'
    $statusOutput = & $python $Server status @CompanionArgs
    $isRunning = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = 'Stop'
    if ($isRunning) {
        $statusOutput | Write-Output
        Write-Output 'A companion server is already running (see -Status, -Attach, -Stop).'
        exit 0
    }
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    # The hidden window is still a console: --no-attach and --no-live keep the companion from
    # attaching to another one or drawing its panel there. Its log goes to companion-server.log.
    $argList = @("`"$Server`"", '--launched-by', "`"$LaunchedBy`"", '--no-attach', '--no-live')
    if ($CompanionArgs) { $argList += @($CompanionArgs | ForEach-Object { "`"$_`"" }) }
    # Start-Process sees the venv python.exe's PID, a launcher for the real interpreter: -Status shows the companion's.
    Start-Process -FilePath $python -ArgumentList $argList -WorkingDirectory $AfmRoot `
        -WindowStyle Hidden -RedirectStandardError $ErrLogFile | Out-Null
    Write-Output "Started the companion server in the background with $python (see -Status)."
    Write-Output "Log: $(Join-Path $StateDir 'companion-server.log') (watch it with -Attach, stop it with -Stop)"
    exit 0
}

Write-Output "Starting the companion server with $python (Ctrl-C stops it)"
Set-Location -LiteralPath $AfmRoot
& $python $Server @CompanionArgs
exit $LASTEXITCODE
