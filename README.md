# ChatGPT Automation (nodriver)

Автоматическая отправка промпта в ChatGPT и сбор ответа через браузер Chrome с использованием `nodriver`.

## Архитектура

Скрипт использует **копию профиля Chrome** для сохранения авторизации:

1. Из `%LOCALAPPDATA%\Google\Chrome\User Data` копируются:
   - `Local State` — файл с ключами шифрования куков (DPAPI)
   - `Profile 2` — папка профиля с cookies и сессией
2. Копия хранится в `chrome_work_profile/` внутри проекта
3. Chrome запускается через nodriver с этим рабочим профилем

## Требования

- **Windows** (DPAPI привязан к текущему пользователю)
- **Python 3.10+** (venv: `.venv\Scripts\python.exe`)
- **Google Chrome** установлен
- Авторизация в ChatGPT в **Profile 2** оригинального Chrome

## Быстрый старт

### 1. Установка зависимостей

```cmd
.venv\Scripts\pip.exe install -r requirements.txt
```

### 2. Первый запуск (если нет рабочего профиля)

Скрипт автоматически скопирует профиль при первом запуске.

**Важно:** Перед запуском закройте ВСЕ окна Chrome!

```cmd
taskkill /F /IM chrome.exe
.venv\Scripts\python.exe main.py
```

### 3. Если сессия истекла (кнопка Login)

Запустите `login_chrome.bat`, войдите в ChatGPT вручную, **полностью закройте браузер**, затем запустите `main.py` снова.

Если хотите полностью обновить профиль:
```cmd
rmdir /s /q chrome_work_profile
.venv\Scripts\python.exe main.py
```

## Файлы проекта

| Файл | Описание |
|---|---|
| `main.py` | Основной скрипт автоматизации |
| `login_chrome.bat` | Ручной вход в Chrome с рабочим профилем |
| `requirements.txt` | Зависимости Python |
| `result.txt` | Последний ответ ChatGPT (создаётся автоматически) |
| `chrome_work_profile/` | Рабочая копия профиля Chrome |

## Как это работает

1. Копирует `Local State` + `Profile 2` → `chrome_work_profile/`
2. Запускает Chrome (видимое окно, не headless)
3. Переходит на `chatgpt.com`, проверяет авторизацию
4. Находит поле ввода, печатает промпт посимвольно (имитация человека)
5. Отправляет промпт, ждёт завершения генерации (DOM-polling)
6. Извлекает текст последнего ответа assistant
7. Сохраняет в `result.txt`, закрывает браузер

## Устранение проблем

- **«Сессия недействительна»** → Запустите `login_chrome.bat`, войдите, закройте браузер
- **Chrome не запускается** → Убейте все процессы: `taskkill /F /IM chrome.exe`
- **Профиль повреждён** → Удалите `chrome_work_profile/` и запустите заново
