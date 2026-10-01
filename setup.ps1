# Установка ИИ-помощника на сервер. Запускать один раз из папки проекта
# под тем пользователем Windows, под которым выполнен вход в Claude Code:
#
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#
# Повторный запуск безопасен: уже сделанное пропускается.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root
function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "   $t" -ForegroundColor Green }
function Warn($t) { Write-Host "   $t" -ForegroundColor Yellow }

Step "Python и зависимости"
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "Не найден Python 3.11+. Установите с python.org (галочка «Add to PATH») и запустите снова." }
& $python -m pip install --disable-pip-version-check -q -r requirements.txt
Ok "готово"

Step "Конфиги"
foreach ($n in "settings", "users", "bases") {
    $dst = "config\$n.yaml"
    if (-not (Test-Path $dst)) { Copy-Item "config\$n.example.yaml" $dst; Ok "создан $dst" } else { Ok "$dst уже есть" }
}
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Ok "создан .env — впишите в него доступы" }

Step "Cloudflared (HTTPS-туннель без домена и сертификата)"
$cf = Join-Path $root "tools\cloudflared.exe"
if (-not (Test-Path $cf)) {
    New-Item -ItemType Directory -Force (Join-Path $root "tools") | Out-Null
    Invoke-WebRequest "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile $cf
    Ok "скачан tools\cloudflared.exe"
} else { Ok "уже есть" }

Step "Claude Code"
$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
    Warn "claude не найден — ставлю официальным установщиком Anthropic"
    Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    Warn "Выполните в этом окне: claude  → /login (вход в подписку), затем запустите setup.ps1 ещё раз"
    exit 0
}
Ok (& claude --version)

Step "База знаний"
& $python scripts\build_kb_index.py

Step "Проверка Claude Code с настройками бота"
& $python scripts\check_claude.py
if ($LASTEXITCODE -ne 0) { Warn "проверка не прошла — см. вывод выше (часто: не выполнен /login)" }

Step "Автозапуск"
$task = "ИИ-помощник ГК Эксперт"
if (-not (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue)) {
    $pythonw = Join-Path (Split-Path $python) "pythonw.exe"
    if (-not (Test-Path $pythonw)) { $pythonw = $python }
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "scripts\start.py" -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
    Write-Host "   Для запуска при старте сервера нужен пароль пользователя $env:USERNAME (вводится только в Windows)."
    $cred = Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" -Message "Пароль Windows для автозапуска помощника"
    Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings `
        -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited | Out-Null
    Ok "задача «$task» создана: запуск при старте сервера, без окна консоли"
} else { Ok "задача «$task» уже есть" }

Step "Запуск"
Start-ScheduledTask -TaskName $task
Start-Sleep 20
Get-Content "logs\start.log" -Tail 10 -ErrorAction SilentlyContinue
Write-Host "`nЛоги: logs\start.log, logs\bridge.log, logs\tunnel.log. Статистика: python scripts\stats.py"
