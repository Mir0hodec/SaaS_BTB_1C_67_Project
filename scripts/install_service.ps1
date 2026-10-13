# Ставит ИИ-помощника системной задачей Windows «Claude CLI bot».
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1   (от имени администратора)
#
# Бот работает от имени системы: запускается вместе с сервером, не зависит от того,
# кто вошёл на сервер, и поднимается сам, если упал. Повторный запуск безопасен.
# Ещё ставит ярлыки на общий рабочий стол (видны всем пользователям) и закрывает
# папку со входом в Claude (.claude-bot) от всех, кроме системы и администраторов.
$ErrorActionPreference = "Stop"
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

$root = Split-Path $PSScriptRoot -Parent
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Нет $py — сначала создайте окружение .venv" }
$port = Get-BotPort $root

Write-Host "== Останавливаю бота, запущенного вручную"
Stop-BotProcesses $root

Write-Host "== Вход в Claude и токены баз 1С: доступ только системе и администраторам"
foreach ($name in ".claude-bot", ".secrets") {
    $dir = Join-Path $root $name
    New-Item -ItemType Directory -Force $dir | Out-Null
    icacls $dir /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
}

Write-Host "== Системная задача «$TaskName»"
$action = New-ScheduledTaskAction -Execute $py -Argument "-X utf8 scripts\start.py" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "== Ярлыки на общем рабочем столе"
$desk = [Environment]::GetFolderPath("CommonDesktopDirectory")
& (Join-Path $PSScriptRoot "panel_shortcut.ps1") -Port $port   # панель: в папке бота и на рабочем столе
Set-Content -Encoding ASCII (Join-Path $desk "Запустить ИИ-помощника.cmd") "@powershell -NoProfile -ExecutionPolicy Bypass -File `"$root\scripts\start_bot.ps1`""
Set-Content -Encoding ASCII (Join-Path $desk "Сменить аккаунт Claude.cmd") "@powershell -NoProfile -ExecutionPolicy Bypass -File `"$root\scripts\login_claude.ps1`""
Set-Content -Encoding UTF8 "C:\Панель ИИ-помощника.txt" @(
    "Панель управления ИИ-помощником ГК «Эксперт»",
    "",
    "Адрес: http://127.0.0.1:$port/panel",
    "Открывать в Edge или Chrome на этом сервере (не в Internet Explorer), под любым пользователем. Пароль не нужен: снаружи сервера панель не открывается.",
    "",
    "Бот работает системной задачей Windows «$TaskName»: запускается вместе с сервером и не зависит от того, кто вошёл.",
    "Ярлык панели — в папке бота: $root\! Панель управления ИИ-помощника.",
    "Ярлыки на рабочем столе (у всех пользователей):",
    "  «Панель ИИ-помощника» — открыть панель;",
    "  «Запустить ИИ-помощника» — запустить или перезапустить бота, если панель не открывается;",
    "  «Сменить аккаунт Claude» — войти в другой аккаунт Claude."
)

if (Wait-Bot $port 60) { Write-Host "Готово: бот работает, панель — http://127.0.0.1:$port/panel" -ForegroundColor Green }
else { Write-Host "Бот не ответил за минуту — смотрите logs\start.log и logs\bridge.log" -ForegroundColor Yellow }
