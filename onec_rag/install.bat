@echo off
rem 1C code search installer. Arguments are passed to install.ps1, e.g.:
rem   install.bat -RepoPath "D:\1c-dumps\base" -RepoName base
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
