# Скиллы 1С для бота: сборка внешних обработок и отчётов на сервере (MCP-сервер dev)
# и обработка MCP Toolkit для баз сотрудников.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_1c_skills.ps1   (от имени администратора)
#
# Ставит в tools\ закреплённые версии:
#   cc-1c-skills    — github.com/Nikolay-Shirokov/cc-1c-skills (сборка .epf/.erf, формы, СКД, макеты);
#   1c-mcp-toolkit  — github.com/ROCTUP/1c-mcp-toolkit (MCP_Toolkit.epf и скилл composing-1c-queries).
# Python-зависимости скиллов (lxml, Pillow, psutil) — в .venv бота. Повторный запуск безопасен.
param(
    [string]$SkillsCommit = "b102d9dad19304f35e1d0639aa719a60b5ab9f49",
    [string]$ToolkitCommit = "dba3f22e292f576e745ea6ed76773d18a512557a"
)
$ErrorActionPreference = "Stop"

$p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`""
    exit
}

$root = Split-Path $PSScriptRoot -Parent
$tools = Join-Path $root "tools"
New-Item -ItemType Directory -Force $tools | Out-Null

function Get-Repo([string]$url, [string]$dir, [string]$commit) {
    if (-not (Test-Path (Join-Path $dir ".git"))) {
        Write-Host "== git clone $url"
        git -c safe.directory=* clone --quiet $url $dir
    } else {
        Write-Host "== git fetch $url"
        git -c safe.directory=* -C $dir fetch --quiet origin
    }
    git -c safe.directory=* -C $dir checkout --quiet $commit
    if ($LASTEXITCODE -ne 0) { throw "не удалось переключиться на $commit в $dir" }
    Write-Host "   $(git -c safe.directory=* -C $dir log -1 --format='%h %cs %s')"
}

Get-Repo "https://github.com/Nikolay-Shirokov/cc-1c-skills" (Join-Path $tools "cc-1c-skills") $SkillsCommit
Get-Repo "https://github.com/ROCTUP/1c-mcp-toolkit" (Join-Path $tools "1c-mcp-toolkit") $ToolkitCommit

Write-Host "== Python-зависимости скиллов"
& (Join-Path $root ".venv\Scripts\python.exe") -m pip install --disable-pip-version-check -q "lxml>=4.9.0" "Pillow>=10.0.0" "psutil>=5.9.0"
if ($LASTEXITCODE -ne 0) { throw "pip install не выполнен" }

Write-Host "== Обработка MCP Toolkit для сотрудников"
Copy-Item (Join-Path $tools "1c-mcp-toolkit\build\MCP_Toolkit.epf") (Join-Path $tools "MCP_Toolkit.epf") -Force
Copy-Item (Join-Path $tools "1c-mcp-toolkit\build\MCP_Toolkit_x86.epf") (Join-Path $tools "MCP_Toolkit_x86.epf") -Force

Write-Host "Готово. Бот подхватит инструменты сборки со следующего вопроса; проверка — панель, «Проверки» → «Сборка обработки»." -ForegroundColor Green
