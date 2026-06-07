<#
.SYNOPSIS
  Регистрирует apiproxy как Windows Service через NSSM.
  Запускать от администратора.
#>

$ErrorActionPreference = "Stop"
$RootDir = Split-Path $PSScriptRoot -Parent
$NssmPath = "$RootDir\tools\nssm.exe"
$ServiceName = "apiproxy"
$PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) { throw "python не найден в PATH" }

# Скачать NSSM если нет
if (-not (Test-Path $NssmPath)) {
    New-Item -ItemType Directory -Force "$RootDir\tools" | Out-Null
    $zipPath = "$env:TEMP\nssm.zip"
    Write-Host "Скачиваю NSSM..."
    Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $zipPath -UseBasicParsing
    Expand-Archive $zipPath "$env:TEMP\nssm_extract" -Force
    $nssmExe = Get-ChildItem "$env:TEMP\nssm_extract" -Recurse -Filter "nssm.exe" |
        Where-Object { $_.FullName -match "win64" } | Select-Object -First 1
    if (-not $nssmExe) { $nssmExe = Get-ChildItem "$env:TEMP\nssm_extract" -Recurse -Filter "nssm.exe" | Select-Object -First 1 }
    Copy-Item $nssmExe.FullName $NssmPath
    Remove-Item $zipPath -Force
    Remove-Item "$env:TEMP\nssm_extract" -Recurse -Force
    Write-Host "NSSM установлен: $NssmPath"
}

# Удалить старый сервис если есть
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Удаляю старый сервис $ServiceName..."
    & $NssmPath stop $ServiceName 2>$null
    & $NssmPath remove $ServiceName confirm
}

# Зарегистрировать
Write-Host "Регистрирую сервис $ServiceName..."
& $NssmPath install $ServiceName $PythonExe "run.py"
& $NssmPath set $ServiceName AppDirectory $RootDir
& $NssmPath set $ServiceName DisplayName "ChatGPT API Proxy"
& $NssmPath set $ServiceName Description "Human-mimic ChatGPT proxy for Kilo Code"
& $NssmPath set $ServiceName Start SERVICE_AUTO_START
& $NssmPath set $ServiceName AppStdout "$RootDir\logs\service.log"
& $NssmPath set $ServiceName AppStderr "$RootDir\logs\service.log"
& $NssmPath set $ServiceName AppRotateFiles 1
& $NssmPath set $ServiceName AppRotateBytes 10485760
& $NssmPath set $ServiceName AppRestartDelay 3000

Write-Host ""
Write-Host "✅ Сервис зарегистрирован. Запуск:"
Write-Host "   Start-Service $ServiceName"
Write-Host "   или: & '$NssmPath' start $ServiceName"
Write-Host ""
Write-Host "Управление браузерами (когда сервис запущен):"
Write-Host "   Показать: Invoke-RestMethod http://localhost:47821/admin/browsers/show -Method Post"
Write-Host "   Скрыть:   Invoke-RestMethod http://localhost:47821/admin/browsers/hide -Method Post"
