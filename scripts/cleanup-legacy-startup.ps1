# 下线 phase-0 之前的旧机制(VBS 自启 / 桌面 bat)
$ErrorActionPreference = 'Continue'

$candidates = @(
    "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\RAGScheduler.vbs",
    "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\RAG-Audit.vbs",
    "$env:USERPROFILE\Desktop\RAG-Manager.bat",
    "$env:USERPROFILE\Desktop\RAG-Shutdown.ps1",
    'D:\RAG\RAG-Manager.bat',
    'D:\RAG\RAG-Shutdown.ps1'
)

foreach ($p in $candidates) {
    if (Test-Path $p) {
        Write-Host "[remove] $p"
        Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "[skip ] $p (absent)"
    }
}

# 创建桌面快捷方式 -> Panel
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'RAG Panel.url'
@"
[InternetShortcut]
URL=http://127.0.0.1:8840/
IconIndex=0
"@ | Set-Content -LiteralPath $lnk -Encoding ASCII
Write-Host "[create] $lnk"

Write-Host 'done.'
