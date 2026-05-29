# Запуск прокси с логированием. Под капотом — scripts/run_tee.py (надёжнее PS-пайпа).
# Использование: powershell -ExecutionPolicy Bypass -File scripts\run_with_logs.ps1

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& python scripts\run_tee.py
exit $LASTEXITCODE
