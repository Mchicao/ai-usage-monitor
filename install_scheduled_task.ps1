$ErrorActionPreference = 'Stop'
$taskName = 'AI Usage Monitor'
$vbsPath  = 'C:\Proyectos\ai-usage-monitor\launch_tray_widget.vbs'
$workdir  = 'C:\Proyectos\ai-usage-monitor'
$user     = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute 'C:\Windows\System32\wscript.exe' -Argument ('"' + $vbsPath + '"') -WorkingDirectory $workdir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) -ExecutionTimeLimit (New-TimeSpan -Hours 0)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Widget de cuotas IA (Z.AI/Codex/AG) - arranca al login' -Force | Out-Null

Write-Output 'Task registrada OK:'
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State, Author | Format-List
Write-Output 'Triggers:'
(Get-ScheduledTask -TaskName $taskName).Triggers | Format-List
Write-Output 'Actions:'
(Get-ScheduledTask -TaskName $taskName).Actions | Format-List
