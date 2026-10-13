# Ярлык панели управления ИИ-помощником — со своим значком и подписью:
#   * в корне папки бота, первым в списке: «! Панель управления ИИ-помощника»;
#   * на общем рабочем столе (виден всем пользователям): «Панель ИИ-помощника».
#
#   powershell -ExecutionPolicy Bypass -File scripts\panel_shortcut.ps1   (от имени администратора)
#
# Панель открывается в Edge или Chrome: ярлык .url открылся бы в браузере по умолчанию,
# а на сервере это бывает Internet Explorer, где панель не работает.
param([int]$Port = 0)
$ErrorActionPreference = "Stop"

$p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`""
    exit
}

$root = Split-Path $PSScriptRoot -Parent
if (-not $Port) {
    $m = Select-String -Path (Join-Path $root "config\settings.yaml") -Pattern '^port:\s*(\d+)' -ErrorAction SilentlyContinue
    $Port = if ($m) { [int]$m.Matches[0].Groups[1].Value } else { 8010 }
}
$panelUrl = "http://127.0.0.1:$Port/panel"
$icon = Join-Path $root "bridge\panel_ui\panel.ico"
$browser = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
             "$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
$desk = [Environment]::GetFolderPath("CommonDesktopDirectory")

function New-PanelShortcut([string]$path) {
    Remove-Item $path, ([IO.Path]::ChangeExtension($path, ".url")) -ErrorAction SilentlyContinue
    if (-not $browser) {
        Set-Content -Encoding ASCII ([IO.Path]::ChangeExtension($path, ".url")) "[InternetShortcut]`r`nURL=$panelUrl"
        return
    }
    $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
    $lnk.TargetPath = $browser
    $lnk.Arguments = $panelUrl
    $lnk.Description = "Панель управления ИИ-помощником ГК «Эксперт»: $panelUrl"
    if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
    $lnk.Save()
}

New-PanelShortcut (Join-Path $root "! Панель управления ИИ-помощника.lnk")
New-PanelShortcut (Join-Path $desk "Панель ИИ-помощника.lnk")
Write-Host "Ярлык панели: $root\! Панель управления ИИ-помощника и на рабочем столе — $panelUrl" -ForegroundColor Green
