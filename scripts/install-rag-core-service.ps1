# 注册 rag-core 为 NSSM Windows Service
# 用法: 1) 下载 nssm.exe 放到 D:\RAG\tools\
#       2) 管理员 PowerShell 跑这个脚本
#
# nssm.exe 下载: https://nssm.cc/release/nssm-2.24.zip (取 win64\nssm.exe)
# 或 chocolatey: choco install nssm

$ErrorActionPreference = 'Stop'

$NSSM      = 'D:\RAG\tools\nssm.exe'
$SVC_NAME  = 'rag-core'
$PYTHON    = if (Get-Command python -ErrorAction SilentlyContinue) {
    & python -c "import sys; print(sys.executable)"
} else {
    throw "python not found on PATH"
}
$WORKDIR   = 'D:\RAG\rag-core'
$ARGS      = '-B main.py'
$LOG_OUT   = 'D:\RAG\logs\rag-core.out.log'
$LOG_ERR   = 'D:\RAG\logs\rag-core.err.log'

if (-not (Test-Path $NSSM)) { throw "nssm.exe not found at $NSSM" }

# 管理员权限检查
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "must run as Administrator" }

# 如果已有同名服务先删
$svc = Get-Service $SVC_NAME -ErrorAction SilentlyContinue
if ($svc) {
    Write-Host "removing existing service $SVC_NAME"
    if ($svc.Status -ne 'Stopped') { & $NSSM stop $SVC_NAME confirm | Out-Null }
    & $NSSM remove $SVC_NAME confirm | Out-Null
    Start-Sleep 2
}

# 杀掉前台跑的 python(如果有)
Get-WmiObject Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*main.py*" -and $_.CommandLine -like "*rag-core*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Host "installing $SVC_NAME"
& $NSSM install $SVC_NAME $PYTHON $ARGS
& $NSSM set $SVC_NAME AppDirectory       $WORKDIR
& $NSSM set $SVC_NAME DisplayName        'RAG Core (scheduler+audit+sync+panel)'
& $NSSM set $SVC_NAME Description        'RAG core unified service on 127.0.0.1:8840'
& $NSSM set $SVC_NAME Start              SERVICE_AUTO_START
& $NSSM set $SVC_NAME DependOnService    'com.docker.service'
& $NSSM set $SVC_NAME AppStdout          $LOG_OUT
& $NSSM set $SVC_NAME AppStderr          $LOG_ERR
& $NSSM set $SVC_NAME AppRotateFiles     1
& $NSSM set $SVC_NAME AppRotateOnline    1
& $NSSM set $SVC_NAME AppRotateBytes     5242880
& $NSSM set $SVC_NAME AppExit Default    Restart
& $NSSM set $SVC_NAME AppRestartDelay    3000
& $NSSM set $SVC_NAME AppStopMethodSkip  0
& $NSSM set $SVC_NAME AppStopMethodConsole 5000

Write-Host "starting $SVC_NAME"
& $NSSM start $SVC_NAME

Start-Sleep 4
Get-Service $SVC_NAME | Format-List Name, Status, StartType
Write-Host "verify: curl http://127.0.0.1:8840/health"
