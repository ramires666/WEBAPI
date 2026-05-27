@echo off
chcp 65001 >nul
echo ============================================
echo   ChatGPT - Ручной вход в Chrome
echo ============================================
echo.
echo Закройте ВСЕ окна Google Chrome перед запуском!
echo.
echo Открываем Chrome с рабочим профилем...
echo После авторизации ПОЛНОСТЬЮ закройте браузер.
echo.
start chrome --user-data-dir="W:\_python\APIPROXY\chrome_work_profile" --profile-directory="Profile 2" "https://chatgpt.com"
pause
