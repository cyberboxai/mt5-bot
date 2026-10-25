# Registers a Windows Task Scheduler task that starts the bot supervisor when you log on
# (so also after a reboot, once Windows logs you in). "Start in" is this project folder.
# Usage (normally through install_autostart.bat):
#   powershell -NoProfile -ExecutionPolicy Bypass -File install_autostart.ps1 [-DelayMinutes 2] [-BotArgs "--dry-run"]
param(
    [string]$TaskName = "MT5 Bot Supervisor",
    [string]$BotArgs = "",
    [int]$DelayMinutes = 1
)
$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Launcher = Join-Path $ProjectDir "start_supervisor.bat"
if (-not (Test-Path $Launcher)) { throw "start_supervisor.bat not found in $ProjectDir" }
if (-not (Test-Path (Join-Path $ProjectDir ".venv\Scripts\python.exe"))) {
    throw "Virtual environment .venv not found in $ProjectDir. Follow the README installation section first."
}

$User = "$env:USERDOMAIN\$env:USERNAME"
if ($BotArgs) {
    $Action = New-ScheduledTaskAction -Execute $Launcher -Argument $BotArgs -WorkingDirectory $ProjectDir
} else {
    $Action = New-ScheduledTaskAction -Execute $Launcher -WorkingDirectory $ProjectDir
}
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$Trigger.Delay = "PT$($DelayMinutes)M"
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
# Interactive: runs in your desktop session, where the MT5 terminal also runs.
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Principal $Principal -Description "Starts the MT5 bot supervisor at logon" -Force | Out-Null

Write-Host "Task '$TaskName' registered."
Write-Host "  Program : $Launcher $BotArgs"
Write-Host "  Start in: $ProjectDir"
Write-Host "  Trigger : at logon of $User, delayed $DelayMinutes minute(s)"
Write-Host "Test it now with: schtasks /Run /TN `"$TaskName`""
