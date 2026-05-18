# 注册 rag-tray 为登录时启动的 Task Scheduler 任务
# 不能用 NSSM(托盘图标必须在用户会话)
# 不需要管理员权限,普通用户即可创建自己 session 下的任务

$ErrorActionPreference = 'Stop'

$TASK_NAME = 'rag-tray'
$BAT       = 'D:\RAG\rag-tray\start-tray.bat'

if (-not (Test-Path $BAT)) { throw "tray launcher not found: $BAT" }

# 删除已存在的同名任务
$existing = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "removing existing task $TASK_NAME"
    Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false
}

$action  = New-ScheduledTaskAction -Execute $BAT
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TASK_NAME `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'RAG tray icon (pystray, right-click for actions)'

Write-Host "registered task $TASK_NAME, trying to start it now..."
Start-ScheduledTask -TaskName $TASK_NAME
Start-Sleep 2
Get-ScheduledTask -TaskName $TASK_NAME | Format-List TaskName, State
