@echo off
REM Запуск прокси с логированием. Под капотом — scripts/run_tee.py.
setlocal
cd /d "%~dp0\.."
python scripts\run_tee.py
endlocal
