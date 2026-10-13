# Подключение своей копии базы 1С к ИИ-помощнику ГК «Эксперт» (запускать на ПК сотрудника).
#
#   Правой кнопкой → «Выполнить с помощью PowerShell»  (попросит права администратора)
#   или: powershell -ExecutionPolicy Bypass -File employee_toolkit.ps1 -BasePath "D:\Базы\ГКЭ"
#
# Что делает:
#   1. Разрешает в брандмауэре порт MCP Toolkit (6003) — только для сервера помощника.
#   2. Создаёт на рабочем столе ярлык «1С + MCP Toolkit (<база>)»: открывает базу под
#      «Администратор» с паролем 1 и сразу запускает MCP Toolkit во встроенном режиме.
#   3. Показывает адрес, который нужно передать администратору помощника.
# Рабочий пароль сюда не вводится: в копии базы у «Администратор» должен быть пароль 1.
param(
    [string]$BasePath = "",                  # каталог файловой базы (где лежит 1Cv8.1CD)
    [string]$BaseName = "",                  # имя для ярлыка
    [int]$Port = 6003,
    [string]$BotServer = "192.168.0.47",     # сервер ИИ-помощника — только ему открывается порт
    [string]$EpfPath = ""                    # MCP_Toolkit.epf; пусто — рядом со скриптом
)
$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $args2 = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-File", "`"$PSCommandPath`"")
    if ($BasePath) { $args2 += @("-BasePath", "`"$BasePath`"") }
    if ($BaseName) { $args2 += @("-BaseName", "`"$BaseName`"") }
    Start-Process powershell -Verb RunAs -ArgumentList $args2
    exit
}

Add-Type -AssemblyName System.Windows.Forms
if (-not $BasePath) {
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = "Выберите папку своей копии базы 1С (в ней лежит файл 1Cv8.1CD)"
    if ($dlg.ShowDialog() -ne "OK") { Write-Host "Отменено."; exit 1 }
    $BasePath = $dlg.SelectedPath
}
if (-not (Test-Path (Join-Path $BasePath "1Cv8.1CD"))) { throw "В папке $BasePath нет файла 1Cv8.1CD — это не файловая база 1С" }
if (-not $BaseName) { $BaseName = Split-Path $BasePath -Leaf }

# Платформа: самая новая 8.3 — сначала 64-разрядная, потом 32-разрядная.
function Find-Platform([string]$root) {
    Get-ChildItem (Join-Path $root "1cv8") -Directory -Filter "8.3.*" -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName "bin\1cv8.exe") } |
        Sort-Object { [version]$_.Name } | Select-Object -Last 1
}
$x64 = Find-Platform $env:ProgramFiles
$x86 = Find-Platform ${env:ProgramFiles(x86)}
$platform = if ($x64) { $x64 } else { $x86 }
if (-not $platform) { throw "Не найдена платформа 1С 8.3 в Program Files\1cv8" }
$exe = Join-Path $platform.FullName "bin\1cv8.exe"
$is64 = [bool]$x64

if (-not $EpfPath) { $EpfPath = Join-Path $PSScriptRoot ($(if ($is64) { "MCP_Toolkit.epf" } else { "MCP_Toolkit_x86.epf" })) }
if (-not (Test-Path $EpfPath)) { throw "Нет файла $EpfPath — получите его у помощника командой /toolkit" }
$epfDir = Join-Path $env:LOCALAPPDATA "AiHelper1C"
New-Item -ItemType Directory -Force $epfDir | Out-Null
$epf = Join-Path $epfDir (Split-Path $EpfPath -Leaf)
Copy-Item $EpfPath $epf -Force

Write-Host "== Брандмауэр: порт $Port только для $BotServer"
$rule = "AI helper MCP Toolkit $Port"
Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $rule -Direction Inbound -Protocol TCP -LocalPort $Port -RemoteAddress $BotServer -Action Allow | Out-Null

Write-Host "== Ярлык на рабочем столе"
$startup = "startup;mode=embedded;port=$Port;auth=password;pwd=1"
$arguments = "ENTERPRISE /F `"$BasePath`" /N `"Администратор`" /P `"1`" /Execute `"$epf`" /C `"$startup`""
$lnkPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "1С + MCP Toolkit ($BaseName).lnk"
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $exe
$lnk.Arguments = $arguments
$lnk.WorkingDirectory = Split-Path $exe
$lnk.Description = "База $BaseName с MCP Toolkit для ИИ-помощника"
$lnk.Save()

$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } | Select-Object -Expand IPAddress
Write-Host ""
Write-Host "Готово." -ForegroundColor Green
Write-Host "Ярлык: $lnkPath"
Write-Host "Дальше:"
Write-Host " 1. Откройте базу ярлыком. Если 1С скажет, что пароль неверный, — задайте в копии базы пользователю Администратор пароль 1."
Write-Host " 2. В окне MCP Toolkit: в первый раз «Сгенерировать» токен. Чтобы помощник исправлял данные — внизу, в блоке «Автоматически разрешать операции», галочка «Записать». Окно не закрывайте."
Write-Host " 3. Передайте администратору помощника:"
foreach ($ip in $ips) { Write-Host ("      адрес: http://{0}:{1}/mcp" -f $ip, $Port) -ForegroundColor Cyan }
Write-Host "      и токен из окна MCP Toolkit."
