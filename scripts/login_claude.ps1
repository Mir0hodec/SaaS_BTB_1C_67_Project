# Войти в другой аккаунт Claude для бота. Ярлык «Сменить аккаунт Claude» на общем рабочем столе.
# Вход хранится в папке бота .claude-bot — бот подхватывает его со следующего вопроса.

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

$host.UI.RawUI.WindowTitle = "Claude: аккаунт бота"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$env:CLAUDE_CONFIG_DIR = Join-Path $root ".claude-bot"
Write-Host ""
Write-Host "Смена аккаунта Claude для ИИ-помощника" -ForegroundColor Cyan
Write-Host " 1. В окне Claude введите /login и нажмите Enter (если Claude сам предложит войти — следуйте подсказкам)."
Write-Host " 2. Выберите «Claude account with subscription»."
Write-Host " 3. Ссылку из окна откройте в Edge, войдите в нужный аккаунт и нажмите Authorize."
Write-Host " 4. Код со страницы вставьте сюда правой кнопкой мыши и нажмите Enter."
Write-Host " 5. Когда вход пройдёт, закройте окно. Новый аккаунт виден в панели управления."
Write-Host ""
& (Join-Path $root "tools\claude.exe")
