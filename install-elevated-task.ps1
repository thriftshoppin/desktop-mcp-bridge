# OPTIONAL FALLBACK - the user runs this ONCE from an elevated PowerShell; the bridge never runs it.
# Creates an on-demand scheduled task (Run with highest privileges, in YOUR interactive session so the worker window is visible)
# that launcher.py starts if ShellExecute 'runas' ever fails. The launcher writes %LOCALAPPDATA%\desktop-bridge\handoff.json first;
# the worker reads it (pipe name, token, launcher pid) and sends it to the Recycle Bin.
#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py   = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "venv not found at $py" }
$action    = New-ScheduledTaskAction -Execute $py -Argument '-m desktop_bridge.worker --handoff' -WorkingDirectory $root
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'DesktopBridgeWorker' -Action $action -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Registered task 'DesktopBridgeWorker' (on demand, highest privileges, visible window)." -ForegroundColor Green
Write-Host "Remove it later with: Unregister-ScheduledTask -TaskName DesktopBridgeWorker -Confirm:`$false"
