# Register (or re-register) the SamsungTVBridge boot service and start it.
# Runs as SYSTEM at startup so the bridge is up before anyone logs in and
# survives disconnects/reboots. Idempotent - safe to run repeatedly.
$ErrorActionPreference = "Stop"

$py   = "C:\Program Files\Python312\pythonw.exe"
$dir  = "C:\tvbridge"
$task = "SamsungTVBridge"

if (-not (Test-Path $py))  { throw "pythonw not found at $py" }
if (-not (Test-Path "$dir\tvbridge.py")) { throw "tvbridge.py not found in $dir" }

# 1) tear down anything already running/registered
Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue |
    Unregister-ScheduledTask -Confirm:$false
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -in @("python.exe","pythonw.exe") -and $_.CommandLine -like "*tvbridge*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2

# 2) register: SYSTEM, at boot, highest, no run-time cap
$action  = New-ScheduledTaskAction -Execute $py -Argument "`"$dir\tvbridge.py`" run" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtStartup
$princ   = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
             -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger `
    -Principal $princ -Settings $set | Out-Null

# 3) start now and verify
Start-ScheduledTask -TaskName $task
Start-Sleep 4

$c = Get-NetTCPConnection -State Listen -LocalPort 8899 -ErrorAction SilentlyContinue
if ($c) {
    $p = Get-Process -Id $c.OwningProcess
    $u = (Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)" |
          Invoke-CimMethod -MethodName GetOwner).User
    Write-Output "LISTENING pid=$($p.Id) name=$($p.ProcessName) user=$u"
} else {
    Write-Output "NOT LISTENING - checking task result"
    Get-ScheduledTaskInfo -TaskName $task |
        Select-Object LastRunTime, LastTaskResult | Format-List
}
