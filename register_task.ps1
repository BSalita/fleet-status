# Start fleet-status when you sign in to Windows (scheduled task "FleetStatus").
#
#   powershell -File register_task.ps1 [-Python C:\path\to\pythonw.exe] [-Remove]
#
# Runs "pythonw server.py" (no window) at sign-in with no time limit, and starts it again up to three
# times if it stops. Page: http://127.0.0.1:8540/ (or the port in config.json).
param([string]$Python = '', [switch]$Remove)
$ErrorActionPreference = 'Stop'
$taskName = 'FleetStatus'
if ($Remove) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task $taskName."
    return
}
if (-not $Python) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { throw 'pythonw.exe not on PATH: pass -Python C:\path\to\pythonw.exe' }
    $Python = $cmd.Source
}
$script = Join-Path $PSScriptRoot 'server.py'
foreach ($p in $Python, $script, (Join-Path $PSScriptRoot 'config.json')) {
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { throw "Not found: $p" }
}
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$script`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'fleet-status: live page of your machines and jobs on http://127.0.0.1:8540/' -Force | Out-Null
Write-Host "Registered $taskName (starts at sign-in). Start it now: Start-ScheduledTask $taskName"
