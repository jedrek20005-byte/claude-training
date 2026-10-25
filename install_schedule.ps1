# Creates a Windows scheduled task that runs the bot every weekday at 16:30 (Polish time).
# The US market is open then all year (it opens 14:30 or 15:30 Polish time and closes 21:00 or 22:00).
# If the PC is off at 16:30, the task runs as soon as possible after it is turned on;
# the bot itself skips trading when the market is closed.
$ErrorActionPreference = "Stop"
$bat = Join-Path $PSScriptRoot "run_bot.bat"
$action = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "16:30"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName "ClaudeTradingBot" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Scheduled task 'ClaudeTradingBot' created: weekdays at 16:30."
Write-Host "Remove it any time with:  Unregister-ScheduledTask -TaskName ClaudeTradingBot"
