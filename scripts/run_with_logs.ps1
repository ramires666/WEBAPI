# Запуск прокси с логированием. Под капотом — scripts/run_tee.py (надёжнее PS-пайпа).
# Использование: powershell -ExecutionPolicy Bypass -File scripts\run_with_logs.ps1

chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& python scripts\run_tee.py
exit $LASTEXITCODE
