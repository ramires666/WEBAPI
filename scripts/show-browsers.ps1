# Вывести браузеры прокси на экран (для логина / проверки)
try {
    $r = Invoke-RestMethod "http://localhost:47821/admin/browsers/show" -Method Post
    Write-Host "✅ Браузеры выведены на экран. Логинься, потом запусти hide-browsers.ps1"
} catch {
    Write-Host "❌ Не удалось: $_"
    Write-Host "   Убедись что сервис запущен: Get-Service apiproxy"
}
