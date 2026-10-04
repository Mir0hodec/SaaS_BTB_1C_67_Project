# 1C code search (onec_rag) installer for Windows. Safe to re-run.
#
#   powershell -ExecutionPolicy Bypass -File onec_rag\install.ps1 -RepoPath "D:\1c-dumps\base" -RepoName base
#
# What it does: creates onec_rag\.venv, installs the "mcp" package, creates config.json,
# builds the index for the first time and registers a Task Scheduler job that runs
# "git pull" + incremental re-index every N minutes. No services, no background processes.
# This file is ASCII-only on purpose: Windows PowerShell 5.1 misreads UTF-8 without BOM.
param(
    [string]$RepoPath = "",          # folder with the 1C configuration dump (a git working copy)
    [string]$RepoName = "base",      # short latin name, used in links: base/Documents/...:10-40
    [int]$IntervalMinutes = 10,
    [string]$TaskName = "OnecCodeIndex",
    [switch]$NoTask,                 # do not touch Task Scheduler
    [switch]$CurrentUserOnly         # task runs only while this user is logged on (no password prompt)
)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "   $t" -ForegroundColor Green }
function Warn($t) { Write-Host "   $t" -ForegroundColor Yellow }

Step "Python"
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "Python 3.10+ not found. Install it from python.org (check 'Add to PATH') and re-run." }
& $python -c "import sys, sqlite3; assert sys.version_info >= (3, 10), 'Python 3.10+ required'; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x, tokenize=''trigram'')')"
if ($LASTEXITCODE -ne 0) { throw "This Python is older than 3.10 or its SQLite has no FTS5 trigram tokenizer (needs SQLite 3.34+). Install a current Python." }
Ok (& $python --version)

Step "Virtual environment"
$venv = Join-Path $here ".venv"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) { & $python -m venv $venv; Ok "created $venv" } else { Ok "already exists" }
& $py -m pip install --disable-pip-version-check -q -r (Join-Path $here "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Ok "package mcp installed"

Step "Config"
$config = Join-Path $here "config.json"
if (-not (Test-Path $config)) {
    if (-not $RepoPath) {
        Copy-Item (Join-Path $here "config.example.json") $config
        Warn "created $config from the example. Set repos[].path to the dump folder and re-run install.ps1"
        exit 0
    }
    # Written by Python: correct JSON escaping and UTF-8 for cyrillic paths.
    $env:ONEC_SETUP_REPO_PATH = (Resolve-Path -LiteralPath $RepoPath).Path
    $env:ONEC_SETUP_REPO_NAME = $RepoName
    $env:ONEC_SETUP_CONFIG = $config
    $env:ONEC_SETUP_EXAMPLE = Join-Path $here "config.example.json"
    & $py -X utf8 -c "import json, os; c = json.load(open(os.environ['ONEC_SETUP_EXAMPLE'], encoding='utf-8')); c['repos'] = [{'name': os.environ['ONEC_SETUP_REPO_NAME'], 'path': os.environ['ONEC_SETUP_REPO_PATH'].replace(chr(92), '/'), 'git_pull': True}]; json.dump(c, open(os.environ['ONEC_SETUP_CONFIG'], 'w', encoding='utf-8'), ensure_ascii=False, indent=2)"
    if ($LASTEXITCODE -ne 0) { throw "could not write config.json" }
    Ok "created $config"
} else { Ok "$config already exists (not changed)" }

Step "Git settings for long and cyrillic paths"
if ($RepoPath -and (Test-Path -LiteralPath (Join-Path $RepoPath ".git")) -and (Get-Command git -ErrorAction SilentlyContinue)) {
    & git -C $RepoPath config core.longpaths true
    & git -C $RepoPath config core.quotepath false
    Ok "core.longpaths=true, core.quotepath=false in $RepoPath"
} else { Warn "skipped (no -RepoPath, no .git there, or git is not installed). The indexer itself handles long paths." }

Step "First index build"
$sw = [Diagnostics.Stopwatch]::StartNew()
$env:PYTHONUTF8 = "1"
& $py (Join-Path $here "onec_index.py") --config $config
if ($LASTEXITCODE -ne 0) { throw "indexer failed, see the output above" }
Ok ("done in {0:n0} s" -f $sw.Elapsed.TotalSeconds)

if ($NoTask) { Warn "Task Scheduler step skipped (-NoTask)"; exit 0 }

Step "Task Scheduler: git pull + re-index every $IntervalMinutes min"
$pyw = Join-Path $venv "Scripts\pythonw.exe"          # no console window
if (-not (Test-Path $pyw)) { $pyw = $py }
$arguments = '"{0}" --config "{1}"' -f (Join-Path $here "onec_index.py"), $config
$action = New-ScheduledTaskAction -Execute $pyw -Argument $arguments -WorkingDirectory $here
$start = (Get-Date).AddMinutes(1)
$interval = New-TimeSpan -Minutes $IntervalMinutes
try { $trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval $interval }
catch { $trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval $interval -RepetitionDuration (New-TimeSpan -Days 3650) }
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
if ($CurrentUserOnly) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings | Out-Null
    Ok "task '$TaskName' registered: runs while $env:USERNAME is logged on"
} else {
    Write-Host "   The task must run when nobody is logged on and 'git pull' needs this user's git credentials,"
    Write-Host "   so Windows asks for the password of $env:USERNAME (it is stored by Task Scheduler only)."
    $cred = Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" -Message "Windows password for the scheduled task"
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited | Out-Null
    Ok "task '$TaskName' registered: every $IntervalMinutes min, also when nobody is logged on"
}

Write-Host "`nLog: logs\onec_index.log. Index state: $py onec_index.py --stats. Quality check: $py eval.py --generate 30, then $py eval.py"
Write-Host "The bot picks the tools up automatically on the next question (bridge sees onec_rag\config.json)."
