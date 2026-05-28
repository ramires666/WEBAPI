# ChatGPT API Proxy — Резюме состояния проекта

**Дата:** 28.05.2026  
**Путь проекта:** `W:\_python\APIPROXY`  
**Репозиторий:** git, ветка `main`  
**Точка входа:** `python run.py` → uvicorn на `127.0.0.1:8000`

---

## Что это

Локальный прокси-сервер, который выставляет **OpenAI-совместимый API** (`/v1/chat/completions`), а под капотом управляет **реальной браузерной сессией ChatGPT** через Chrome DevTools Protocol (CDP). Клиент — IDE-расширение **Kilo Code** (также совместим с Cline, Cursor, Roo Code, Aider и др.).

**Зачем:** бесплатный доступ к ChatGPT из IDE-агентов, которые умеют работать только с OpenAI API.

---

## Архитектура

```
IDE-агент (Kilo Code / Cline / Cursor)
    │  HTTP POST /v1/chat/completions (OpenAI формат)
    ▼
┌─────────────────────────────────────────────┐
│  proxy/api_server.py  (FastAPI, :8000)      │
│  ├── ConversationStore.match()              │
│  │   └── SQLite: маппинг диалогов → URL     │
│  ├── prompt_optimizer: стрип system prompt  │
│  └── format_delta_prompt(): склейка дельты  │
└────────────────┬────────────────────────────┘
                 │  plain text prompt
                 ▼
┌─────────────────────────────────────────────┐
│  browser/browser_manager.py                 │
│  ├── nodriver (undetected Chrome)           │
│  ├── select_model(Instant/Thinking)         │
│  ├── human_type / insert_text_fast (CDP)    │
│  └── poll DOM → yield text chunks           │
└────────────────┬────────────────────────────┘
                 │  raw text stream
                 ▼
┌─────────────────────────────────────────────┐
│  browser/cdp_native.py                      │
│  └── read_last_assistant(page)              │
│      JS evaluate → парсинг DOM              │
│      div[data-message-author-role=assistant] │
└────────────────┬────────────────────────────┘
                 │  raw text (с делимитерами)
                 ▼
┌─────────────────────────────────────────────┐
│  proxy/response_parser.py                   │
│  <<<WRITE>>> <<<BASH>>> <<<EDIT>>> и т.д.   │
│  → OpenAI tool_calls SSE chunks             │
└────────────────┬────────────────────────────┘
                 │  data: {"choices":[...]}\n\n
                 ▼
            SSE stream → IDE-агент
```

---

## Структура файлов

```
W:\_python\APIPROXY\
├── run.py                          # Entrypoint (6 строк, uvicorn)
├── config.py                       # Конфиг: пути Chrome, таймауты
├── requirements.txt                # fastapi, nodriver, loguru
│
├── proxy/                          # API-слой
│   ├── api_server.py               # FastAPI, POST /v1/chat/completions, GET /v1/models
│   ├── conversation_store.py       # SQLite: маппинг сессий IDE → URL чатов ChatGPT
│   ├── prompt_optimizer.py         # Стрип system prompt, фильтр tools
│   └── response_parser.py          # Парсинг <<<WRITE>>> делимитеров → tool_calls
│
├── browser/                        # Браузерный слой
│   ├── browser_manager.py          # Оркестрация: ввод, стрим, модель
│   ├── cdp_native.py               # Низкоуровневый CDP: клики, ввод, чтение DOM
│   └── file_extractor.py           # Скачивание файлов из ChatGPT
│
├── scripts/
│   ├── tooltest.py                 # Тест-харнесс: python scripts/tooltest.py "задача"
│   ├── check_hashes.py             # Дебаг хешей conversation_store
│   ├── fix_profile.ps1             # Починка профиля Chrome
│   └── login_chrome.bat            # Логин в Chrome
│
├── docs/
│   └── custom_instructions.txt     # Текст для ChatGPT Custom Instructions (КРИТИЧНО!)
│
├── temp/                           # Дампы, логи, скрэтч
│   ├── request_dump_*.json         # Дампы каждого входящего запроса
│   ├── server.log                  # Лог uvicorn
│   └── final_generation_dump.txt   # Последний полный ответ ChatGPT
│
├── chrome_work_profile/            # Рабочая копия Chrome-профиля
├── temp_downloads/                 # Скачанные файлы из ChatGPT
├── proxy_state.db                  # SQLite БД conversation_store
└── CLAUDE.MD                       # Инструкции для Claude (не для нас)
```

---

## Ключевые механизмы

### 1. Делимитер-протокол (вместо JSON)

ChatGPT **не умеет** стабильно генерировать JSON с эскейпом многострочного кода. Поэтому используется **делимитерный протокол** — модель пишет сырой контент внутри `<<<VERB>>>...<<<END>>>` блоков:

| Делимитер | OpenAI tool | Аргументы |
|-----------|-------------|-----------|
| `<<<WRITE path="...">>>...<<<END>>>` | `write` | `{filePath, content}` |
| `<<<EDIT path="...">>><<<OLD>>>...<<<NEW>>>...<<<END>>>` | `edit` | `{filePath, oldString, newString}` |
| `<<<BASH>>>...<<<END>>>` | `bash` | `{command}` |
| `<<<READ path="...">>>` | `read` | `{filePath}` |
| `<<<GLOB pattern="...">>>` | `glob` | `{pattern}` |
| `<<<GREP pattern="..." path="...">>>` | `grep` | `{pattern, path}` |

Протокол задан в **Custom Instructions аккаунта ChatGPT** (файл [docs/custom_instructions.txt](file:///w:/_python/APIPROXY/docs/custom_instructions.txt)), а НЕ в system prompt запроса — так модель стабильнее следует формату.

### 2. Conversation Store (продолжение чатов)

[conversation_store.py](file:///w:/_python/APIPROXY/proxy/conversation_store.py) — SQLite-маппинг. Каждый новый запрос из IDE:
1. Хешируются USER-сообщения (MD5), с вырезанием `<environment_details>`
2. Ищется существующий чат по префиксу хешей **внутри одного client_id** (kilocode, cline, cursor и т.д.)
3. Если найден → `is_new_chat=False`, берётся `chat_url`, delta = сообщения после последнего assistant
4. Если нет → `is_new_chat=True`, весь массив сообщений как delta

> **Бесконечный цикл (пофикшен):** Kilo агентский — шлёт всю историю. Без дельты прокси перечитывал экран → тот же ответ → ∞. Фикс: delta = только **после последнего assistant** (tool-результаты / новый user).

### 3. System Prompt Optimization

[prompt_optimizer.py](file:///w:/_python/APIPROXY/proxy/prompt_optimizer.py):
- **System message:** Полный system prompt Kilo (~13k символов) заменяется на **только** `<env>...</env>` блок (пути, ОС). Протокол tool-use уже в Custom Instructions аккаунта.
- **Tools:** Фильтруются до 8 разрешённых (`read, glob, grep, edit, write, bash, task, question`). Описания обрезаются до 200 символов.

### 4. Стриминг ответов

[browser_manager.py](file:///w:/_python/APIPROXY/browser/browser_manager.py) → `send_prompt_and_stream()`:
- Поллинг DOM каждые 0.4с, до `GENERATION_TIMEOUT=300с`
- **Стабильность:** yield только текст, совпавший в **ДВУХ подряд** чтениях DOM (исключает ре-рендеры)
- Детекция завершения: текст != baseline, Stop-кнопка исчезла, текст стабилен ≥2с
- Досыл хвоста после завершения генерации
- Человекоподобный скролл в цикле ожидания (1.5–3.5с интервал)

### 5. Модели

Прокси выставляет 2 модели: `Instant` и `Thinking`. Переключение реальным кликом по кнопке `button[aria-haspopup="menu"]` в интерфейсе ChatGPT.

---

## Что сделано (хронология)

### ✅ 28.05.2026 — Реорганизация профилей Chrome в `profiles/` + чистка

Цель: подготовить пул профилей под мультибраузерный режим. До этого все прогретые профили валялись в корне (`Profile 2/`, `Profile 4/`, `Profile_Fixed/`), а код копировал профиль из реального Chrome (`%LOCALAPPDATA%\Google\Chrome\User Data\Profile 2`).

**FS-операции:**
- Создан каталог `profiles/`.
- Перемещены: `Profile 2/`, `Profile 4/`, `Profile_Fixed/` → `profiles/<имя>/`.
- Структура осталась как была (гибридный Profile 2 не перекладывал — option 1).

**Чистка кэшей/метрик/preload (то что Chrome регенерирует на 1-м запуске):**
- Кэши: `Cache`, `Code Cache`, `GPUCache`, `GraphiteDawnCache`, `DawnGraphiteCache`, `DawnWebGPUCache`, `GrShaderCache`, `ShaderCache`, `Service Worker/CacheStorage`, `Service Worker/ScriptCache`.
- Метрики/краш: `Crashpad`, `BrowserMetrics`, `DeferredBrowserMetrics`, `BrowserMetrics-spare.pma`, `CrashpadMetrics-active.pma`.
- CRX-кэши: `Component CRX Cache`, `component_crx_cache`, `extensions_crx_cache`.
- ML/модели/preload: `AutofillAiModelCache`, `AutofillStrikeDatabase`, `OnDeviceHeadSuggestModel`, `OptimizationHints`, `optimization_guide_*`, `Feature Engagement Tracker`, `Subresource Filter`, `segmentation_platform`, `ActorSafetyLists`, `AmountExtractionHeuristicRegexes`, `CaptchaProviders`, `CertificateRevocation`, `Crowd Deny`, `FileTypePolicies`, `FirstPartySetsPreloaded`, `MEIPreload`, `OriginTrials`, `PKIMetadata`, `PrivacySandboxAttestationsPreloaded`, `RecoveryImproved`, `SSLErrorAssistant`, `Safe Browsing`, `SafetyTips`, `TrustTokenKeyCommitments`, `Variations`, `WasmTtsEngine`, `WidevineCdm`, `hyphen-data`, `ZxcvbnData`, `MediaDeviceSalts`.
- Tracking/история: `BrowsingTopicsSiteData*`, `BrowsingTopicsState`, `DIPS*`, `Top Sites*`, `Visited Links`, `JumpListIcons*`, `Download Service`, `DownloadMetadata`, `InterestGroups*`, `DataSharing`, `Network Action Predictor*`, `Sessions`, `Session Storage`.
- LOG/LOCK файлы.

**Оставлено (auth-критичное и human-mimic):**
- `Network/Cookies` (+ journal), `Network/Trust Tokens*`, `Network/TransportSecurity`, `Network/Network Persistent State` — Chrome v96+ хранит cookies в `Network/`, не в корне профиля!
- `Local Storage/`, `IndexedDB/`, `WebStorage/`, `Service Worker/Database`.
- `Login Data*`, `Login Data For Account*`, `Web Data*`, `Preferences`, `Secure Preferences`, `Trust Tokens*`.
- `Local State` (только в User Data-root shape — Profile 2, Profile_Fixed; там хранятся encryption keys для cookies).
- `Bookmarks*`, `Favicons*`, `History*`, `Web Data*` — мелочи для прикрытия "человечности".
- `Google Profile.ico`, `Google Profile Picture.png`, `First Run`, `Last Browser`, `Last Version`.
- `Sync Data/`, `Sync App Settings/`, `Accounts/`, `GCM Store` — Google account state.

**Результат:**
- `Profile 2`: 541M → 212M (-61%)
- `Profile 4`: 1.1G → 186M (-83%)
- `Profile_Fixed`: 530M → 207M (-61%)
- **Итого: 2.2G → 605M (-1.6G)**

**Не тронуто:**
- `chrome_work_profile/` (runtime working copy, пересоздаётся на старте) — пока остаётся в корне.
- Код (`browser/browser_manager.py`, `config.py`) — ещё ссылается на `%LOCALAPPDATA%\Google\Chrome\User Data` через `ORIGINAL_CHROME_USER_DATA`. Под новую структуру `profiles/` будет переписан в следующей сессии (см. Фаза 3 ниже).

---

## 🚧 Фаза 3 (актуальная): Пул профилей, round-robin, камуфляж

Цели:
1. Не «задрачивать» один аккаунт ChatGPT — распределять нагрузку по 2+ профилям.
2. Каждый новый чат начинается на **следующем** профиле (round-robin).
3. При срабатывании авто-саммари (Компонент 3) — новый чат после саммари тоже **на следующем** профиле (продолжение работы в свежем браузере, разгрузка предыдущего).
4. Камуфляж делимитер-протокола: вместо очевидных `<<<WRITE>>>` — человекоподобные/русские/кодовые маркеры, чтобы при выборочной проверке аккаунта в OpenAI чат не палился как «инструментальный».

### 3.1 Структура (готова, см. сегодняшнюю реорганизацию)

```
profiles/
├── Profile 2/       (гибрид, 212M)
├── Profile 4/       (single-profile, 186M)
└── Profile_Fixed/   (User Data-root, 207M, легаси — возможно убрать после теста новых)
```

### 3.2 Конфиг (план)

`.env`:
```
PROFILES=Profile 2,Profile 4          # порядок round-robin
PROFILES_DIR=W:/_python/APIPROXY/profiles
WORK_DIR=W:/_python/APIPROXY/work     # рабочие копии (заменит chrome_work_profile)
```

`config.py`:
- `PROFILES: list[str]` — из `.env`, парсится через `split(",")`.
- `PROFILES_DIR: str` — корень с прогретыми профилями.
- `WORK_DIR: str` — куда копируются рабочие.

### 3.3 BrowserPool (план)

Заменяет одиночный `BrowserManager`. Архитектура:

- На старте: для каждого имени из `PROFILES` поднимается отдельный `BrowserManager` (свой `user_data_dir = WORK_DIR/<name>`, копируется из `PROFILES_DIR/<name>`).
- Все браузеры держатся **постоянно открытыми** и залогиненными.
- Round-robin указатель `_next_idx`: при `is_new_chat=True` выбирается `pool[_next_idx]`, индекс инкрементируется по модулю.
- **Sticky-биндинг**: в `conversations` таблицу добавляется колонка `browser_id` (имя профиля). При продолжении чата (`is_new_chat=False`) — берётся тот же браузер, где чат был начат. Иначе разные браузеры не видят URL друг друга.
- Запросы к разным браузерам идут **параллельно** (каждый со своим asyncio.Lock). К одному — сериализуются.
- При авто-саммари: `is_new_chat:=True` после саммари, далее round-robin как обычно → новый чат уходит на следующий профиль.

Изменения:
- `browser/browser_pool.py` — новый.
- `browser/browser_manager.py` — параметризовать `__init__(profile_name, work_dir, profiles_dir)`, убрать глобальные импорты.
- `proxy/api_server.py` — `browser_manager` → `browser_pool`; в `chat_completions` выбор браузера до `match()` или после, со sticky-проверкой.
- `proxy/conversation_store.py` — `+browser_id TEXT` колонка + миграция; `match()` возвращает `browser_id`; `upsert()` принимает.
- `run.py` — без изменений.

### 3.4 Камуфляж делимитеров (план)

Проблема: `<<<WRITE path="...">>>...<<<END>>>` — очевидно автоматизация при ручной/выборочной проверке аккаунта. Если OpenAI просканит чат глазами или regex-фильтром — флаг.

Варианты маркеров (на выбор пользователя):

**A. Условные русские слова** — наименее палится, выглядит как «странный пользователь пишет на русском»:
- `<<<ЗАПИСЬ путь="...">>> ... <<<КОНЕЦ>>>`
- `<<<ПРАВКА путь="...">>> <<<СТАРОЕ>>>...<<<НОВОЕ>>>...<<<КОНЕЦ>>>`
- `<<<ОБОЛОЧКА>>>...<<<КОНЕЦ>>>`

**B. Кодовое слово + параметр** — выглядит как кастомный markdown пользователя:
- `:::файл создать="..."::: ... :::конец:::`
- `:::файл правка="..."::: ... :::конец:::`
- `:::оболочка::: ... :::конец:::`

**C. Невидимые/полуневидимые маркеры** — Unicode-разделители (U+2063, U+2064) или редкие emoji:
- `📝[путь]...📍` — emoji-маркеры (бросаются в глаза при чтении глазами, но не палятся как «инструменты»)

**D. Естественноязычные команды** — модель пишет «Создаю файл X со следующим содержимым: ... », парсер ловит regex:
- `Создаю файл "<путь>":\n<контент>\n[Конец файла]`
- Самое незаметное при проверке глазами, но требует более хитрый regex; модель легче ломается.

Изменения:
- `proxy/response_parser.py` — `_HEADER_RE`, `_BLOCK_VERBS`, `_END` + `_split_old_new` под новые маркеры (одно место).
- `docs/custom_instructions.txt` — переписать описание протокола под новые слова (это и есть аккаунт-левел систем-промпт, его надо вставить в ChatGPT Settings вручную).

### 3.5 Решения пользователя (28.05.2026)

- **Камуфляж: вариант B** — `:::файл создать="..."::: ... :::конец:::`, `:::файл правка="..."::: :::старое::: ... :::новое::: ... :::конец:::`, `:::оболочка::: ... :::конец:::`. Похоже на pandoc fenced-div, тихо при чтении глазами.
- **Приоритет следующей сессии: BrowserPool** (3.2 + 3.3). Камуфляж — после.
- **Profile_Fixed остаётся в пуле**: `PROFILES=Profile 2,Profile 4,Profile_Fixed`. Если на 1-м запуске окажется не залогинен — выкинуть из конфига.

### 3.6 Порядок реализации (следующая сессия)

1. ✅ **СДЕЛАНО 28.05** — BrowserPool (3.2 + 3.3): коммит `99caeb8 feat: BrowserPool with round-robin + sticky binding`. Верификация прошла: round-robin Profile 2 → 4 → Fixed → 2..., sticky `browser_id` roundtrip через ConversationStore работает, импорт api_server чистый.
2. End-to-end тест на реальном запуске: 3 последовательных new-chat запроса → каждый уходит в свой профиль, все 3 чата живут параллельно. **Ждёт ручного запуска.**
3. Триггер авто-саммари (искусственно занизить `TOKEN_LIMIT` в .env) → проверить что новый чат после саммари уходит на следующий профиль. **Ждёт ручного запуска.**
4. Микроплан 3.4 (камуфляж B `:::файл создать="...":::`).

---

## 🚧 Фаза 4: Расширение тулзов (после камуфляжа B)

Текущий набор делимитеров (6): `WRITE`, `EDIT`, `BASH`, `READ`, `GLOB`, `GREP`. Дисбаланс: `execute_command`, `task`, `question`, `background_process` в `ALLOWED_TOOLS` но без делимитеров — модель их видит в спеке, но не вызовет.

### 4.1 Tier 1 — базовая работа с файлами (критично)

| Делимитер (камуфляж B) | Native tool | Аргументы |
|---|---|---|
| `:::каталог список="...":::` | `list_directory` | `{path}` — структурированный листинг (name, type, size) |
| `:::файл правки="..."::: :::блок::: OLD:::НА::: NEW :::блок::: OLD2:::НА::: NEW2 :::конец:::` | `multi_edit` | `{filePath, edits: [{oldString, newString}, ...]}` — N правок одним вызовом |
| `:::файл удалить="...":::` | `delete` | `{path}` (безопаснее `bash rm`) |
| `:::файл переместить="src" в="dst":::` | `move` | `{source, destination}` |
| `:::папка создать="...":::` | `mkdir` | `{path}` с `-p` семантикой |

### 4.2 Tier 2 — агентская продуктивность

| Делимитер | Native tool | Что делает |
|---|---|---|
| `:::патч применить="..."::: …unified diff… :::конец:::` | `apply_patch` | `git apply`-style для крупных рефакторингов (устойчивее EDIT с длинным OLD/NEW) |
| `:::получить url="...":::` | `web_fetch` | **Уникально:** тянет URL **через залогиненный Chrome** прокси → доступ к приватной документации / SaaS-API |
| `:::задача добавить="...":::`, `:::задача завершить="...":::` | `todo_write` | Менеджер задач для длинных циклов |

### 4.3 Чистка `ALLOWED_TOOLS`

Решить: реализовать делимитеры под `execute_command`, `task`, `question`, `background_process` ИЛИ убрать из `ALLOWED_TOOLS` (Kilo не будет показывать модели те, которые мы не парсим).

---

## 🚧 Фаза 5: Sub-агенты на Gemini Flash 2.5+ (multi-provider, скорректирована 28.05)

Цель: запускать sub-агентов в **отдельных чатах Gemini Flash** — он быстрее и дешевле ChatGPT, отлично подходит для чёрной работы (поиск, OCR, видео-разбор, парсинг логов, генерация бойлерплейта).

### 5.1 Архитектура — один Chrome, две вкладки

**Ключевое решение пользователя (28.05):** НЕ поднимать отдельный Chrome для Gemini. Использовать **тот же Chrome-инстанс** (= тот же профиль = те же куки), но **две вкладки**:

- Tab 1: `chatgpt.com` — главный агент
- Tab 2: `gemini.google.com/app` — sub-агент

Профиль один — ChatGPT-логин и Google-логин (для Gemini) уживаются в одном Chrome-профиле.

**Anti-bot — переключение фокуса:**
- Перед каждой операцией: `page.bring_to_front()` через CDP (`Page.bringToFront`)
- Иначе вкладка в фоне может пометиться как «inactive» обоими сервисами → детектор activity сработает

**Экономия памяти:** 1 Chrome (~700M) вместо 2 (~1.4G) на каждый профиль. С 3 профилями: 2.1G вместо 4.2G.

### 5.2 Sub-агент УМЕЕТ тулзы (важная коррекция!)

Sub-агент на Gemini **не просто отвечает текстом** — он выполняет **полноценную работу с файлами**: WRITE, EDIT, READ, BASH и т.д. Тот же делимитер-протокол что для ChatGPT (камуфляж B `:::файл создать=...:::`).

Это значит:
- В аккаунте Gemini надо настроить **те же Custom Instructions** что для ChatGPT — текст из [docs/custom_instructions.txt](file:///w:/_python/APIPROXY/docs/custom_instructions.txt), адаптированный под Gemini (отдельная константа `docs/gemini_instructions.txt`).
- `ResponseParser` остаётся общим — делимитеры одинаковые.
- Sub-агент видит **полный контекст**, инжектированный диспатчером (пути файлов, текущая папка, env).

### 5.3 Диспатч sub-агента

Делимитер главного агента: `:::задача описание="..." контекст="file1,file2,file3":::`

Поток:
1. Парсер ловит `:::задача:::` в потоке ChatGPT.
2. **Диспатчер собирает контекст**: читает указанные файлы, склеивает с заголовками `=== FILE: path ===`.
3. **Прокси переключается на Gemini-вкладку** (`gemini_tab.bring_to_front()`), открывает **новый чат**.
4. Инжектится промпт: системный префикс («ты sub-агент, работай через делимитер-протокол») + контекст + задача.
5. Gemini отвечает делимитер-блоками; парсер выполняет тулзы (write/edit/bash и т.д.).
6. Финальный текстовый ответ (после всех тулзов) **возвращается главному агенту** как `tool_call result` (или inline в его поток).
7. Прокси переключается обратно на ChatGPT-вкладку.

### 5.4 Уникальные Gemini-тулзы

Gemini Flash имеет сильные стороны, которых нет у ChatGPT (или хуже):
- **Vision** — распознавание картинок, OCR; быстрое и точное
- **YouTube** — прямой доступ, может пересказать видео по URL
- **Web Search** — встроенный поиск с актуальной выдачей

Новые делимитеры для главного агента (вызывают Gemini вкладку под капотом):

| Делимитер | Поведение |
|---|---|
| `:::картинка путь="...":::` или `:::картинка url="...":::` | Открывает Gemini, прикрепляет картинку, спрашивает «опиши/распознай»; возвращает текст |
| `:::видео url="<youtube>"::: вопрос="...":::` | Gemini пересказывает YouTube видео или отвечает на вопрос о нём |
| `:::поиск запрос="...":::` | Запускает Gemini web search, парсит результаты |

### 5.5 Custom Instructions для Gemini (ручная настройка пользователя)

В аккаунте Google → `gemini.google.com` → Settings → **«Your instructions for Gemini»** (тогглер ВКЛ; пользователь подтвердил наличие 28.05).

Сюда вставляется текст, аналогичный ChatGPT Custom Instructions, **но адаптированный под sub-агентский режим**:
- Тот же делимитер-протокол (камуфляж B)
- Жёстко: «отвечай только тулзами или коротким финальным текстом»
- Запрет «I'm Gemini, here's what I'll do...» преамбул
- Без markdown-обёртки кода если она не нужна для отступов

Текст будет в `docs/gemini_instructions.txt` — параллель к `docs/custom_instructions.txt`.

### 5.6 Plan B — обход через OpenCode + DeepSeek (запасной путь)

Если Gemini-driver окажется слишком хрупким (web UI Google меняется чаще ChatGPT, селекторы ломаются):

**OpenCode** (IDE-агент) имеет режимы:
- **Plan mode** → наш прокси (ChatGPT через делимитеры)
- **Build mode** → DeepSeek API напрямую (платно, но дёшево; быстрая модель уровня Flash)

В этом сценарии sub-агентов мы НЕ реализуем у себя — OpenCode сам разрулит через свои настройки. Прокси остаётся однопровайдерным (только ChatGPT).

**Trade-off:**
- ➖ Платим за DeepSeek (vs Gemini Free)
- ➖ Не используем уникальные фичи Gemini (vision/YouTube/search)
- ➕ В разы меньше работы на нашей стороне
- ➕ Стабильнее (API vs DOM-скрапинг)

Решение Plan A vs Plan B принимается **после первого прототипа GeminiBrowser** — если разведка селекторов окажется лёгкой → Plan A. Если ломается через неделю — Plan B.

### 5.7 Порядок реализации Фазы 5

1. **Разведка Gemini DOM**: вручную залогиниться, посмотреть селекторы композитора / send-кнопки / stop-кнопки / DOM сообщения / индикатор завершения / переключатель модели (Flash vs Pro).
2. **`GeminiBrowser`**: миррор `BrowserManager`, но под `gemini.google.com`. Параметризуется так же (profile_name, profiles_dir, work_dir), но создаёт **2-ю вкладку в существующей Chrome-сессии** вместо нового Chrome.
3. Рефакторинг `BrowserManager` → базовый класс `WebChatBrowser` + два конкретных `ChatGPTBrowser` / `GeminiBrowser`. Каждый профиль = один Chrome = две вкладки (ChatGPT + Gemini).
4. **`subagent_dispatcher.py`**: парсер `:::задача:::` → сбор контекста → инжект в Gemini-вкладку → стрим обратно.
5. **Уникальные Gemini-тулзы** (vision/video/search) — отдельные делимитеры с прицепом картинки/URL.
6. Custom Instructions Gemini: `docs/gemini_instructions.txt`, ручная вставка пользователем в аккаунт.
7. End-to-end тест: главный ChatGPT-чат эмитит `:::задача:::` → Gemini sub-агент пишет файл → результат возвращается → главный продолжает.

---

### ✅ 28.05.2026 — Аудит реализации task.md: все 3 компонента уже в коде

Сессия началась с просмотра `task.md` (4 пункта Компонент 1 + 1 Компонент 2 + 7 Компонент 3 = 12 невыполненных задач). При инспекции обнаружено, что **вся работа уже была сделана в предыдущих коммитах `847c3c9 "saveing"` + `e0083bb "...."`** и в незакоммиченных рабочих изменениях. Галки в `task.md` просто не были проставлены.

**Компонент 1 (форматирование) — закрыт:**
- [browser/cdp_native.py:138](file:///w:/_python/APIPROXY/browser/cdp_native.py) — JS пропускает `DETAILS/SUMMARY/BUTTON` (Thinking-блоки ChatGPT)
- [browser/cdp_native.py:160-161](file:///w:/_python/APIPROXY/browser/cdp_native.py) — после `[Canvas]` обрезается ВЕСЬ хвост (включая дубль кода)
- [browser/cdp_native.py:163](file:///w:/_python/APIPROXY/browser/cdp_native.py) — Python-уровень стрипает `Thinking` / `Thought for N seconds` префикс
- [proxy/response_parser.py:121-129](file:///w:/_python/APIPROXY/proxy/response_parser.py) — `_content()` стрипает Thinking+Canvas, фильтрует пустые чанки (возврат `""` пропускается в `process_stream`)
- [proxy/response_parser.py:89](file:///w:/_python/APIPROXY/proxy/response_parser.py) — `BASH` тулз пропускает команду через `_strip_fence()` (срывает ```bash …``` если модель завернула)

**Компонент 2 (термин. команды) — закрыт:**
- [proxy/prompt_optimizer.py:4-7](file:///w:/_python/APIPROXY/proxy/prompt_optimizer.py) — `ALLOWED_TOOLS` расширен: добавлены `execute_command`, `background_process` (плюс уже были `read/glob/grep/edit/write/bash/task/question`).

**Компонент 3 (токены + авто-саммари) — реализован в working tree, ждёт коммита:**
- `.env` создан на диске (652 байта), добавлен в `.gitignore`.
- [requirements.txt](file:///w:/_python/APIPROXY/requirements.txt) — добавлены `python-dotenv>=1.0.0`, явно прописаны `fastapi`, `uvicorn`.
- [config.py:2,4,14-18](file:///w:/_python/APIPROXY/config.py) — `load_dotenv()` + новые константы: `AUTO_SUMMARY_ENABLED`, `TOKEN_LIMIT` (200000), `SUMMARY_DIR` (`temp/summaries`), `DUMP_MAX_AGE_DAYS` (7).
- [proxy/token_counter.py](file:///w:/_python/APIPROXY/proxy/token_counter.py) — `estimate_tokens()` (cyr/cjk ÷2, latin ÷4) и `estimate_messages_tokens()` с overhead +4 на сообщение. Точность ±15-20%, без tiktoken.
- [proxy/context_summarizer.py](file:///w:/_python/APIPROXY/proxy/context_summarizer.py) — промпт для подробной выгрузки (6 разделов, 2000-5000 слов), `save_summary()` → `temp/summaries/summary_<client_id>_<ts>.md`, `build_context_injection()` оборачивает в маркеры для инъекции в новый чат, `get_latest_summary()`.
- [proxy/api_server.py:11-13, 159-189, 206-207, 239, 259](file:///w:/_python/APIPROXY/proxy/api_server.py) — на каждом запросе считаются токены, при превышении `TOKEN_LIMIT` запускается генерация саммари (через тот же браузер в текущем чате), затем `is_new_chat=True` + `chat_url=None`, и саммари инъектится префиксом в промпт нового чата. `token_count` сохраняется в `conversations.token_count`.
- [proxy/conversation_store.py:74-89, 144-160](file:///w:/_python/APIPROXY/proxy/conversation_store.py) — схема таблицы расширена полями `token_count INTEGER`, `summarized INTEGER`; добавлена идемпотентная миграция `ALTER TABLE ADD COLUMN` для существующих БД (ловит `OperationalError` если колонка уже есть). `upsert()` принимает `token_count`.

**Верификация (28.05 в этой сессии):**
- `estimate_tokens('x'*400)` → 100 (ожидание ~100) ✅
- `estimate_tokens('я'*400)` → 200 (ожидание ~200) ✅
- mixed list/dict messages → корректный счёт ✅
- `config` загружает `.env`, все 4 константы читаются ✅
- `ConversationStore(':memory:')` создаётся, миграция отрабатывает без ошибок ✅
- `ensure_summary_dir()` создаёт `temp/summaries/` ✅
- Импорт всего api_server без ошибок ✅
- `final_generation_dump.txt` (последняя реальная генерация) — чистый, без `Thinking`/`[Canvas]`/`*]()` ✅

**Git state на конец сессии:** 8 modified + 4 untracked файла. Не закоммичено (ждёт решения пользователя — закоммитить одним feat-коммитом или разбить по компонентам). Также `task.md` обновлён — все 12 пунктов отмечены выполненными, оставлен блок «остаточные риски» (ротация дампов, end-to-end тест саммари ≥200k токенов, UTF-8 логи, коммит).

### ✅ 27.05.2026 — Tool Use работает end-to-end
- Делимитер-протокол: `<<<WRITE>>>` → нативный `tool_calls` с валидным контентом
- Рефакторинг: root → пакеты `browser/` + `proxy/`, entrypoint `run.py`
- Фикс: `read_last_assistant` не оборачивает `<PRE>` в ``` (Canvas ломал)
- Фикс: `undefined` в текстовых нодах (`nodeType===3` → `textContent`)
- Фикс: порча символов при стриминге (ре-рендеры DOM вставляли лишние символы)
- Человекоподобный скролл (`human_scroll`)

### ✅ 27.05.2026 — Фикс бесконечного цикла агента
- Delta = сообщения после последнего assistant (а не `users[len(stored):]`)
- `role=="tool"` → `[Результат инструмента]: <текст>`
- Guard: если delta пуста → мгновенный `finish_reason:stop`

### ✅ 27.05.2026 — Фикс отступов (Python)
- Контент файла в ``` фенсе (сохраняет пробелы в `<PRE>`)
- `_strip_fence()` в response_parser снимает обрамление
- Custom Instructions: CRITICAL правило "file content MUST be in fenced code block"

### ✅ 27.05.2026 — Переключение моделей
- Кнопка: `button[aria-haspopup="menu"]`, текст = текущая модель
- Селектор: `[role="menuitemradio"]` (не просто `menuitem`)
- Применяется перед КАЖДЫМ сообщением, не только в новом чате

### ✅ 27.05.2026 — Тест-харнесс
- `scripts/tooltest.py "задача"` — шлёт стрим-запрос с набором тулзов, печатает tool_calls

---

## Известные баги и проблемы (актуальные на 28.05)

### 🔴 Критичные

| # | Баг | Где | Причина |
|---|-----|-----|---------|
| 1 | **Префикс "Thinking" в тексте ответа** | `read_last_assistant()` | DOM ChatGPT содержит метку «Thinking» / «Thought for N seconds» перед основным ответом. JS-код захватывает её как часть текста |
| 2 | **`[Canvas]` + дубль кода** | `read_last_assistant()` | ChatGPT Canvas просачивается: маркер `[Canvas]` снимается, но **дубль кода после него** (в ``` фенсе) остаётся в ответе |
| 3 | **Контекст разрастается бесконтрольно** | `conversation_store` | За одну сессию: 68KB → 267KB. Tool-результаты (полные листинги, файлы) накапливаются. Нет лимита токенов, нет авто-сжатия |

### 🟡 Некритичные

| # | Баг | Где |
|---|-----|-----|
| 4 | Дампы `temp/request_dump_*.json` копятся (уже 80+ файлов, >10MB) | `api_server.dump_request()` |
| 5 | Лог `server.log` в кодировке cp866 (нечитаемый на UTF-8 системах) | loguru / uvicorn stdout |
| 6 | `*]()` (битые цитатные якоря ChatGPT) изредка в контенте | `read_last_assistant()` |
| 7 | Идентичный повторный запрос = продолжение чата → пустая delta → ничего | Дедуп в `conversation_store` |

---

## Запланированные доработки

### 1. Исправление форматирования ответов
- Стрип «Thinking» префикса из DOM-чтения и из response_parser
- Полный стрип Canvas-хвоста (маркер + дубль кода после него)
- Фильтрация пустых content-чанков

### 2. Подсчёт токенов + авто-саммари
- Приблизительный подсчёт `~chars/4` на каждый запрос
- При превышении порога (настраивается, по умолчанию 200k) — автоматическая генерация summary через ChatGPT
- Сохранение summary в `temp/summaries/`
- Запуск нового чата с инъекцией summary как контекста
- Конфиг через `.env` файл

### 3. Расширение поддержки tools
- Добавить `execute_command`, `background_process` в `ALLOWED_TOOLS`
- Фенс-стрип для `<<<BASH>>>` (модель иногда оборачивает команду в ```)

### 4. Ротация дампов
- Автоочистка `temp/request_dump_*.json` старше N дней

---

## Как запустить

### Предварительные условия
1. Chrome с залогиненным аккаунтом ChatGPT (профиль `Profile 2`)
2. **Custom Instructions** ChatGPT настроены по файлу `docs/custom_instructions.txt` (Settings → Personalization → "How would you like ChatGPT to respond")
3. Python 3.10+, venv

### Запуск
```powershell
cd W:\_python\APIPROXY
.venv\Scripts\activate
python run.py
# → Uvicorn running on http://127.0.0.1:8000
```

### Конфиг IDE-клиента (Kilo Code)
```json
{
  "apiProvider": "openai-compatible",
  "baseUrl": "http://127.0.0.1:8000/v1",
  "apiKey": "dummy",
  "model": "Instant"
}
```
Для Thinking: `"model": "Thinking"`

### Тестирование
```powershell
# Тест одного tool call
python scripts/tooltest.py "прочитай файл config.py"

# Тест write
python scripts/tooltest.py "создай файл test123.html с кнопкой"

# ВАЖНО: каждый тест — уникальный текст (дедуп по хешам!)
```

---

## Конфигурация

Файл [config.py](file:///w:/_python/APIPROXY/config.py):

| Константа | Значение | Описание |
|-----------|----------|----------|
| `ORIGINAL_CHROME_USER_DATA` | `%LOCALAPPDATA%\Google\Chrome\User Data` | Откуда копируется профиль |
| `WORKING_PROFILE_DIR` | `W:\_python\APIPROXY\chrome_work_profile` | Рабочая копия профиля |
| `CHROME_PROFILE_NAME` | `Profile 2` | Имя профиля Chrome |
| `CHATGPT_URL` | `https://chatgpt.com` | URL ChatGPT |
| `GENERATION_TIMEOUT` | `300` (сек) | Макс. ожидание генерации |
| `TEMP_DOWNLOADS` | `W:\_python\APIPROXY\temp_downloads` | Папка для скачиваний |
| `TYPING_DELAY` | `(0.01, 0.05)` (сек) | Задержка при посимвольном вводе |

---

## Ключевые зависимости

```
fastapi          # HTTP API
uvicorn          # ASGI сервер
nodriver         # Undetected Chrome (CDP)
loguru           # Логирование
pydantic         # Валидация моделей
```

---

## Важные нюансы для разработчика

1. **Custom Instructions — самая важная часть.** Без них ChatGPT не генерирует делимитер-блоки. Текст в `docs/custom_instructions.txt` нужно вставить вручную в аккаунт ChatGPT.

2. **Canvas — враг.** ChatGPT Canvas (canmore/textdoc) лезет даже при выключенной настройке. Если модель создаёт Canvas, код попадает в `<PRE>` карточку вместо текстового потока. Custom Instructions содержат жёсткое "NEVER use Canvas".

3. **Один запрос за раз.** `BrowserManager` имеет `asyncio.Lock()` — все запросы сериализуются. Это ограничение физического браузера.

4. **Дедуп по хешам** — если отправить тот же текст повторно, `conversation_store` найдёт существующий чат и вернёт пустую дельту → ничего не произойдёт. При тестировании всегда менять текст.

5. **Профиль Chrome** — копируется при старте из `Profile 2` основного Chrome. Если сессия ChatGPT протухла — перелогиниться в основном Chrome и перезапустить прокси (или удалить `chrome_work_profile/`).

6. **Дампы запросов** — каждый входящий запрос дампится в `temp/request_dump_<timestamp>.json`. Полезно для отладки, но копятся. Чистить периодически.

7. **Кодировка логов** — loguru пишет в stdout, uvicorn перенаправляет в лог. На Windows кириллица может быть в cp866. Для нормального чтения: `chcp 65001` в терминале или настроить loguru sink с UTF-8.
