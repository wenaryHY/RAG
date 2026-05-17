# Phase 3 — 服务化与启动整合

## 一次性准备:获取 nssm.exe

任选其一:

1) 官网下载 https://nssm.cc/release/nssm-2.24.zip → 解压 → `win64\nssm.exe` 拷到 `D:\RAG\tools\nssm.exe`
2) chocolatey: `choco install nssm` 然后 `Copy-Item (Get-Command nssm).Source D:\RAG\tools\nssm.exe`

校验:`& 'D:\RAG\tools\nssm.exe' version` 应输出 NSSM 2.24 信息。

## 安装顺序

```powershell
# 1. 管理员 PowerShell 注册 rag-core 为 Windows 服务(开机自启,依赖 Docker)
powershell -ExecutionPolicy Bypass -File D:\RAG\scripts\install-rag-core-service.ps1

# 2. 普通 PowerShell 注册 rag-tray 为登录时启动任务
powershell -ExecutionPolicy Bypass -File D:\RAG\scripts\install-rag-tray-task.ps1

# 3. 清理旧的自启动机制(VBS/桌面 bat)+ 在桌面建 Panel 快捷方式
powershell -ExecutionPolicy Bypass -File D:\RAG\scripts\cleanup-legacy-startup.ps1
```

## 卸载

```powershell
& D:\RAG\tools\nssm.exe stop rag-core; & D:\RAG\tools\nssm.exe remove rag-core confirm
Unregister-ScheduledTask -TaskName rag-tray -Confirm:$false
```

## 验收

- `Get-Service rag-core` → Running, StartType=Automatic
- `Get-ScheduledTask -TaskName rag-tray` → Ready/Running
- 重启 Windows、登录 30 秒内:托盘绿点 + `http://127.0.0.1:8840/` 可访问
