# ChatGPT API Proxy

Локальный прокси: превращает браузерный ChatGPT в OpenAI-совместимый API. Клиент — Kilo Code (IDE-расширение). Браузером управляет нативный CDP без JS-инъекций, что выглядит как живой человек.

## Быстрый старт

```powershell
# Зависимости
pip install -r requirements.txt

# Запуск напрямую
python run.py

# API доступен на http://0.0.0.0:47821/v1
```

## Структура

```
run.py                      — точка входа (uvicorn)
config.py                   — все настройки (env-override)
browser/
  browser_manager.py        — пул вкладок, отправка/стрим, show/hide
  cdp_native.py             — нативный ввод + read_last_assistant
  file_extractor.py         — скачивание файлов из чата
proxy/
  api_server.py             — FastAPI, /v1/chat/completions, admin endpoints
  conversation_store.py     — SQLite-матчинг диалогов по user-хешам
  prompt_optimizer.py       — обрезка системного промпта Kilo (~12k→1.5k)
  response_parser.py        — стрим-парсер делимитер-блоков → нативный tool_calls
  context_summarizer.py     — авто-суммаризация длинных контекстов
scripts/
  install-service.ps1       — регистрация Windows-сервиса через NSSM
  show-browsers.ps1         — вывести браузеры на экран (для ручного логина)
  hide-browsers.ps1         — спрятать браузеры за экран
  login.bat                 — открыть профиль для логина
  run_with_logs.ps1         — запуск с выводом логов
  replay.py                 — реплей реального request_dump для тестов
docs/
  service-setup.md          — полная инструкция по сервису
  custom_instructions.txt   — Custom Instructions для ChatGPT-аккаунта
```

## Конфигурация (.env или env-переменные)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `BIND_HOST` | `0.0.0.0` | Адрес биндинга |
| `BIND_PORT` | `47821` | Порт |
| `API_KEY` | `` (пусто) | Bearer-токен (пусто = без авторизации) |
| `PROFILES` | `Profile 2` | Профили Chrome через запятую |
| `PROFILES_DIR` | `W:\_python\APIPROXY\profiles` | Источник профилей |
| `WORK_DIR` | `W:\_python\APIPROXY\work` | Рабочие копии профилей |
| `TEMP_DOWNLOADS` | `W:\_python\APIPROXY\temp_downloads` | Папка загрузок |

## Браузеры: show/hide

Браузеры стартуют off-screen (`-32000,-32000`) — не мешают, рендерят нормально:

```powershell
.\scripts\show-browsers.ps1   # браузеры выезжают на экран — логинишься руками
.\scripts\hide-browsers.ps1   # уходят обратно
```

Или через API (без авторизации):
```
POST http://localhost:47821/admin/browsers/show
POST http://localhost:47821/admin/browsers/hide
```

## Как сервис (Windows)

```powershell
# От администратора — один раз:
.\scripts\install-service.ps1
Start-Service apiproxy

# Управление:
Stop-Service apiproxy
Restart-Service apiproxy
Get-Service apiproxy
```

Логи: `logs/proxy.log` (ротация по 10 МБ, 5 файлов).

## Настройка Kilo Code

```
Base URL: http://<IP-машины>:47821/v1
Model:    gpt-4o   (или любой — прокси маршрутизирует на ChatGPT)
```

Если `API_KEY` задан — добавить в Kilo: `Authorization: Bearer <ключ>`.

## Custom Instructions (обязательно!)

Протокол работы с файлами (`<<<WRITE>>>`, `<<<EDIT>>>` и т.д.) живёт в **Custom Instructions ChatGPT-аккаунта** (`Settings → Personalization → How would you like ChatGPT to respond`).

Актуальный текст: [`docs/custom_instructions.txt`](docs/custom_instructions.txt)

Без этого модель не будет выдавать делимитер-блоки — tool_calls не появятся.

## Тестирование

```powershell
# Реплей реального дампа запроса (без живого Kilo):
python scripts/replay.py temp/request_dump_XXXXXXX.json

# Живой тест: запустить Kilo, смотреть logs/proxy.log
```

## Делимитер-протокол (кратко)

Модель пишет не JSON, а блоки:

```
<<<WRITE path="src/main.py">>>
... содержимое файла ...
<<<END>>>

<<<EDIT path="config.py">>>
<<<OLD>>>старый фрагмент<<<NEW>>>новый фрагмент<<<END>>>

<<<BASH>>>команда<<<END>>>
<<<READ path="file.py">>>
<<<GLOB pattern="**/*.ts">>>
<<<GREP pattern="TODO" path="src/">>>
<<<MSG>>>обычный текст ответа<<<END>>>
```

`response_parser` переводит это в нативный OpenAI `tool_calls` формат.
