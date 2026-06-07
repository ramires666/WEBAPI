# Убрать браузеры прокси за экран
try {
    $r = Invoke-RestMethod "http://localhost:47821/admin/browsers/hide" -Method Post
    Write-Host "✅ Браузеры убраны за экран."
} catch {
    Write-Host "❌ Не удалось: $_"
    Write-Host "   Убедись что сервис запущен: Get-Service apiproxy"
}
