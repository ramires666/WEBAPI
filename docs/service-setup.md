# Service Setup — ChatGPT API Proxy

## Конфигурация

| Что | Как |
|---|---|
| API порт | **47821** (нестандартный, свободен) |
| Биндинг | `0.0.0.0` → виден по сети |
| Браузеры | off-screen (`-32000,-32000`) — не мешают, рендерят нормально |
| Показать браузеры | `scripts\show-browsers.ps1` или `POST /admin/browsers/show` |
| Скрыть обратно | `scripts\hide-browsers.ps1` |
| Сервис | `scripts\install-service.ps1` (от администратора) |
| Auth | `.env` → `API_KEY=...` (пусто = без авторизации) |

## Установка сервиса (один раз)

```powershell
# От администратора:
.\scripts\install-service.ps1
Start-Service apiproxy
```

## Управление браузерами

```powershell
.\scripts\show-browsers.ps1   # браузеры выезжают на экран
# ... логинишься руками ...
.\scripts\hide-browsers.ps1   # уходят обратно
```

## Настройка Kilo Code

Base URL: `http://<твой-IP>:47821/v1`

Если `API_KEY` задан в `.env` — добавить в Kilo: `Authorization: Bearer <ключ>`

## env-переменные

| Переменная | По умолчанию | Описание |
|---|---|---|
| `BIND_HOST` | `0.0.0.0` | Адрес биндинга |
| `BIND_PORT` | `47821` | Порт |
| `API_KEY` | `` (пусто) | Bearer-токен авторизации |
| `PROFILES` | `Profile 2` | Профили через запятую |
| `PROFILES_DIR` | `W:\_python\APIPROXY\profiles` | Источник профилей |
| `WORK_DIR` | `W:\_python\APIPROXY\work` | Рабочие копии профилей |
| `TEMP_DOWNLOADS` | `W:\_python\APIPROXY\temp_downloads` | Папка загрузок |

## Admin endpoints (без auth)

```
POST http://localhost:47821/admin/browsers/show
POST http://localhost:47821/admin/browsers/hide
```
