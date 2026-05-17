# RAG 服务管理脚本
# 双击 RAG-Manager.bat 启动

$host.UI.RawUI.WindowTitle = "RAG 服务管理"
$ragDir = "D:\RAGFlow\docker"
$lmExe = "D:\softwares\LM Studio\LM Studio.exe"
$schedulerDir = "D:\RAG-Scheduler"
$auditDir = "D:\RAG-Audit"

function Show-Menu {
    Clear-Host
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "       RAG 服务管理" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  开机自启："
    Write-Host "    Docker Desktop + RAGFlow  [自动]"
    Write-Host "    调度层                      [自动]"
    Write-Host ""
    Write-Host "  手动服务："
    Write-Host "    LM Studio                   [用到才开]"
    Write-Host "    审计层                       [手动开启]"
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  [1] 关闭所有服务"
    Write-Host "  [2] 仅关闭 RAGFlow 容器"
    Write-Host "  [3] 仅关闭调度层"
    Write-Host "  [4] 仅关闭 Docker Desktop（含 RAGFlow）"
    Write-Host "  [5] 仅关闭 LM Studio"
    Write-Host "  [6] 启动 RAGFlow"
    Write-Host "  [7] 启动调度层"
    Write-Host "  [8] 启动 LM Studio"
    Write-Host "  [9] 启动审计层"
    Write-Host "  [S] 查看所有状态"
    Write-Host "  [0] 退出"
    Write-Host "========================================" -ForegroundColor Cyan
}

function Stop-RAGFlow {
    Write-Host "`n>>> 关闭 RAGFlow 容器..." -ForegroundColor Yellow
    if (Test-Path $ragDir) {
        Set-Location $ragDir
        docker compose down 2>&1 | Out-Null
        Write-Host "    RAGFlow 已关闭。" -ForegroundColor Green
    } else {
        Write-Host "    未找到 RAGFlow 目录。" -ForegroundColor Gray
    }
}

function Start-RAGFlow {
    Write-Host "`n>>> 启动 RAGFlow 容器..." -ForegroundColor Yellow
    if (Test-Path $ragDir) {
        Set-Location $ragDir
        docker compose up -d 2>&1 | Out-Null
        Write-Host "    RAGFlow 已启动。http://localhost" -ForegroundColor Green
    } else {
        Write-Host "    未找到 RAGFlow 目录。" -ForegroundColor Gray
    }
}

function Stop-Scheduler {
    Write-Host "`n>>> 关闭调度层..." -ForegroundColor Yellow
    $procs = Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*RAG-Scheduler*" -or $_.MainWindowTitle -like "*scheduler*" }
    if ($procs) {
        $procs | Stop-Process -Force
        Write-Host "    调度层已关闭。" -ForegroundColor Green
    } else {
        Write-Host "    调度层未在运行。" -ForegroundColor Gray
    }
}

function Start-Scheduler {
    Write-Host "`n>>> 启动调度层..." -ForegroundColor Yellow
    $procs = Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*RAG-Scheduler*" }
    if ($procs) {
        Write-Host "    调度层已在运行。" -ForegroundColor Green
    } else {
        Start-Process python -ArgumentList "$schedulerDir\main.py" -WindowStyle Hidden
        Write-Host "    调度层已启动。http://127.0.0.1:8850" -ForegroundColor Green
    }
}

function Start-Audit {
    Write-Host "`n>>> 启动审计层..." -ForegroundColor Yellow
    if (-not (Test-Path "$auditDir\main.py")) {
        Write-Host "    审计层代码不存在，请先构建。" -ForegroundColor Red
        return
    }
    $procs = Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*RAG-Audit*" }
    if ($procs) {
        Write-Host "    审计层已在运行。" -ForegroundColor Green
    } else {
        Start-Process python -ArgumentList "$auditDir\main.py" -WindowStyle Hidden
        Write-Host "    审计层已启动。http://127.0.0.1:8860" -ForegroundColor Green
    }
}

function Stop-DockerDesktop {
    Write-Host "`n>>> 关闭 Docker Desktop..." -ForegroundColor Yellow
    Stop-RAGFlow
    $p = Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue
    if ($p) { $p | Stop-Process -Force; Write-Host "    Docker Desktop 已关闭。" -ForegroundColor Green }
    else { Write-Host "    Docker Desktop 未在运行。" -ForegroundColor Gray }
}

function Stop-LMStudio {
    Write-Host "`n>>> 关闭 LM Studio..." -ForegroundColor Yellow
    $p = Get-Process -Name "LM Studio" -ErrorAction SilentlyContinue
    if ($p) { $p | Stop-Process -Force; Write-Host "    LM Studio 已关闭。" -ForegroundColor Green }
    else { Write-Host "    LM Studio 未在运行。" -ForegroundColor Gray }
}

function Start-LMStudio {
    Write-Host "`n>>> 启动 LM Studio..." -ForegroundColor Yellow
    if (Test-Path $lmExe) {
        $p = Get-Process -Name "LM Studio" -ErrorAction SilentlyContinue
        if ($p) { Write-Host "    LM Studio 已在运行。" -ForegroundColor Green }
        else { Start-Process $lmExe; Write-Host "    LM Studio 已启动。" -ForegroundColor Green }
    } else { Write-Host "    未找到 $lmExe" -ForegroundColor Red }
}

function Show-Status {
    Write-Host "`n==================== RAG 运行状态 ====================" -ForegroundColor Cyan

    # Docker Desktop
    $docker = Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue
    Write-Host "  Docker Desktop:     " -NoNewline
    if ($docker) { Write-Host "运行中" -ForegroundColor Green } else { Write-Host "未运行" -ForegroundColor Red }

    # RAGFlow
    if ($docker) {
        Write-Host "  RAGFlow (Docker):" -ForegroundColor DarkGray
        $cs = docker ps --format "table {{.Names}}`t{{.Status}}" 2>$null | Select-String "docker-"
        if ($cs) { $cs | ForEach-Object { Write-Host "    $_" } }
        else { Write-Host "    (无容器运行)" -ForegroundColor Gray }
    }

    # 调度层
    Write-Host "  调度层 (8850):      " -NoNewline
    try { $r = Invoke-RestMethod "http://127.0.0.1:8850/health" -TimeoutSec 3; Write-Host "运行中 ($($r.total_requests)次请求)" -ForegroundColor Green }
    catch { Write-Host "未运行" -ForegroundColor Red }

    # LM Studio
    $lm = Get-Process -Name "LM Studio" -ErrorAction SilentlyContinue
    Write-Host "  LM Studio:          " -NoNewline
    if ($lm) { Write-Host "运行中" -ForegroundColor Green } else { Write-Host "未运行" -ForegroundColor Red }

    # 审计层
    Write-Host "  审计层 (8860):      " -NoNewline
    try { $r = Invoke-RestMethod "http://127.0.0.1:8860/health" -TimeoutSec 3; Write-Host "运行中" -ForegroundColor Green }
    catch { Write-Host "未运行/手动开启" -ForegroundColor Gray }

    Write-Host "====================================================" -ForegroundColor Cyan
}

# 主循环
do {
    Show-Menu
    $choice = Read-Host "请输入选项"
    switch ($choice) {
        "1" { Stop-Scheduler; Stop-RAGFlow; Stop-LMStudio; Stop-DockerDesktop; Write-Host "`n全部服务已关闭。" -ForegroundColor Green }
        "2" { Stop-RAGFlow }
        "3" { Stop-Scheduler }
        "4" { Stop-DockerDesktop }
        "5" { Stop-LMStudio }
        "6" { Start-RAGFlow }
        "7" { Start-Scheduler }
        "8" { Start-LMStudio }
        "9" { Start-Audit }
        "S" { Show-Status }
        "0" { Write-Host "再见。" -ForegroundColor Cyan }
        default { Write-Host "无效选项。" -ForegroundColor Red }
    }
    if ($choice -ne "0") {
        Write-Host "`n按任意键返回菜单..."
        $null = $host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    }
} while ($choice -ne "0")
