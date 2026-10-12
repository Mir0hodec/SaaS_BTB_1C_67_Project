# Запустить или перезапустить ИИ-помощника (системная задача «Claude CLI bot») и открыть панель.
# Ярлык «Запустить ИИ-помощника» на общем рабочем столе.
$TaskName = "Claude CLI bot"

function Test-Admin {
    $p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Get-BotPort([string]$root) {
    $m = Select-String -Path (Join-Path $root "config\settings.yaml") -Pattern '^port:\s*(\d+)' -ErrorAction SilentlyContinue
    if ($m) { return [int]$m.Matches[0].Groups[1].Value } else { return 8010 }
}
function Stop-BotProcesses([string]$root) {
    # Только процессы этого бота (python из C:\Claude CLI) и его окна — старый бот не трогаем.
    Get-CimInstance Win32_Process | Where-Object { $_.Name -like "python*" -and $_.CommandLine -and $_.CommandLine.Contains($root) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Get-Process powershell -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -eq "Claude CLI bot" } |
        Stop-Process -Force -ErrorAction SilentlyContinue
}
function Wait-Bot([int]$port, [int]$seconds = 60) {
    for ($i = 0; $i -lt $seconds; $i += 2) {
        try { if ((Invoke-RestMethod "http://127.0.0.1:$port/health" -TimeoutSec 2).status -eq "ok") { return $true } } catch {}
        Start-Sleep 2
    }
    return $false
}

if (-not (Test-Admin)) {
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`""
    exit
}

$host.UI.RawUI.WindowTitle = "Запуск ИИ-помощника"
$root = Split-Path $PSScriptRoot -Parent
$port = Get-BotPort $root
if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    Write-Host "Нет задачи «$TaskName» — сначала выполните scripts\install_service.ps1" -ForegroundColor Red
    Read-Host "Enter — закрыть"; exit 1
}
Write-Host "Перезапускаю бота…"
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Stop-BotProcesses $root
Start-ScheduledTask -TaskName $TaskName
if (Wait-Bot $port 60) {
    Write-Host "Бот работает." -ForegroundColor Green
    Start-Process msedge "http://127.0.0.1:$port/panel" -ErrorAction SilentlyContinue
    Start-Sleep 3
} else {
    Write-Host "Бот не ответил за минуту — смотрите logs\start.log и logs\bridge.log в $root" -ForegroundColor Yellow
    Read-Host "Enter — закрыть"
}
