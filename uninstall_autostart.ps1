# Removes the Task Scheduler task created by install_autostart.ps1. Does not stop a running bot:
# use the KILL file or Ctrl+C for that (README, section 13).
param([string]$TaskName = "MT5 Bot Supervisor")
$ErrorActionPreference = "Stop"

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $Task) {
    Write-Host "Task '$TaskName' is not registered; nothing to remove."
    exit 0
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Task '$TaskName' removed. The bot will no longer start automatically."
