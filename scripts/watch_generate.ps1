# 监控 posts 目录并在文件变更时自动运行 generate_index.ps1（Node 工具已移除）
# 运行： pwsh .\scripts\watch_generate.ps1
$folder = Join-Path (Get-Location) 'posts'
if(-not (Test-Path $folder)){
    New-Item -ItemType Directory -Path $folder | Out-Null
}

Write-Host "监控： $folder ，在变更时自动运行 generate_index.ps1（Ctrl+C 停止）" -ForegroundColor Green

$scriptPath = Join-Path (Split-Path $MyInvocation.MyCommand.Path) 'generate_index.ps1'
$fsw = New-Object System.IO.FileSystemWatcher $folder -Property @{ IncludeSubdirectories = $false; Filter = '*.html'; NotifyFilter = [System.IO.NotifyFilters]'FileName,LastWrite' }

$action = {
    $e = $Event.SourceEventArgs
    Write-Host "检测到变更： $($e.Name) ($($e.ChangeType))，正在更新索引..." -ForegroundColor Yellow
    Start-Sleep -Milliseconds 200
    & pwsh -NoProfile -File $scriptPath
}

Register-ObjectEvent $fsw Created -Action $action | Out-Null
Register-ObjectEvent $fsw Changed -Action $action | Out-Null
Register-ObjectEvent $fsw Deleted -Action $action | Out-Null

# 先运行一次以确保索引存在
& pwsh -NoProfile -File $scriptPath

# 保持脚本运行以监听事件
while ($true) { Start-Sleep -Seconds 1 }