Workflow rules (binding on Claude)
Microplan first. Any change beyond a literal one-line typo dictated by the user requires a short microplan in chat and explicit approval before any file is touched.
Orchestrate, never code. Claude (orchestrator) must not call Edit/Write on .pine files. All code edits are delegated to a sub-agent via the Agent tool with model: "haiku" (cheapest). Orchestrator inspects (Read/Grep/Bash) and verifies the diff afterwards.
Caveman replies. Chat output: terse fragments, no preamble, no recap. Code comments stay normal.
Meta files are fine to edit directly. CLAUDE.md, memory files, settings — orchestrator can write these without delegating.
Testing default = 3 parallel. Любой e2e-тест прокси — 3 параллельных tooltest.py с уникальными промптами в 3 профиля одновременно. Single-shot прячет race conditions и неравномерности. Сразу мерить тайминги (`📊 ТАЙМИНГ:` по каждому профилю).

---

# Проект: ChatGPT API Proxy (Human-Mimic Edition)

Локальный прокси, выдающий браузерный ChatGPT (управление через нативный CDP, без JS-инъекций ввода) за OpenAI-совместимый API. Клиент — IDE-расширение Kilo Code.

## Точка входа
`python run.py` (uvicorn на 127.0.0.1:8000). Раньше README ссылался на `main.py` — теперь `run.py`.

## Структура (после рефакторинга 2026-05-27)
- `run.py` — entrypoint.
- `config.py` — пути/профиль Chrome/таймауты (root, общий).
- `browser/` — слой браузера: `browser_manager.py` (оркестрация вкладки, отправка/стрим), `cdp_native.py` (нативный ввод + чтение последнего ответа), `file_extractor.py` (скачивание/сбор файлов).
- `proxy/` — API-слой: `api_server.py` (FastAPI, `/v1/chat/completions`), `conversation_store.py` (sqlite-матчинг диалогов по user-хешам, namespace по client_id), `prompt_optimizer.py` (урезание систем-промпта Kilo + фильтр тулзов), `response_parser.py` (стрим: ```json {tool_call} → нативный OpenAI tool_calls).
- `scripts/` — вспомогательные (`check_hashes.py` дебаг дампов, ps1/bat/sh).
- `tests/`, `legacy/`, `docs/`.

## Текущая фаза: дебаг TOOL USE (работа с файлами)
Цель: заставить ChatGPT вызывать `write`/`edit` тулзы, чтобы Kilo писал файлы на диск, а не вываливал код в чат.

Поток: Kilo шлёт систему+tools → `prompt_optimizer` режет систему (12981→~1.5k симв) → ChatGPT возвращает **делимитер-блок** (не JSON) → `response_parser` стримово парсит в нативный `tool_calls` → Kilo исполняет.

**Делимитер-протокол (вместо JSON — для стабильности):** модель пишет сырой контент без эскейпа.
- `<<<WRITE path="...">>>` … `<<<END>>>` → write(filePath, content)
- `<<<EDIT path="...">>>` `<<<OLD>>>` … `<<<NEW>>>` … `<<<END>>>` → edit(filePath, oldString, newString)
- `<<<READ path="...">>>` → read · `<<<BASH>>>`…`<<<END>>>` → bash(command)
- `<<<GLOB pattern="...">>>` / `<<<GREP pattern="..." path="...">>>`
Причина отказа от JSON: модель ломалась на эскейпе многострочного контента в строку (вываливала сырой код в ```json фенс). Делимитер протестирован на разных размерах чанков стрима — стабилен.

### Сделано 2026-05-27 — TOOL USE РАБОТАЕТ end-to-end ✅
Живой тест: запрос «создай html с кнопкой» → модель выдала `<<<WRITE>>>` → прокси отдал **1 нативный `write` tool_call** с валидным `filePath` и целостным контентом (рабочий `<style>`+`<body>`+`<script>`, без ``` фенсов и артефактов). Пайплайн собран.

Ключевые решения:
- **Протокол вынесен в Custom Instructions аккаунта ChatGPT** (Settings → Personalization → «How would you like ChatGPT to respond»). Это аккаунт-левел систем-промпт, жёстче чем in-chat — только так модель стабильно входит в делимитер-режим. В прокси `OPTIMIZED_SYSTEM_PROMPT` больше НЕ шлётся: `optimize_system_message` отдаёт лишь `<env>`-блок. Промпт от прокси ужат до ~размера задачи.
- Убран `PROXY_DIRECTIVE` (противоречил tool-use) и хвостовой `[REMINDER]` (дублировал протокол).
- **Читалка `read_last_assistant` НЕ оборачивает `PRE` в ``` фенсы** — отдаёт сырой код. Причина: ChatGPT Canvas (внутр. инструмент `canmore`/`textdoc`) лезет даже при выключенной настройке и роняет код-карточку (`PRE`) посреди файла; раньше фенс-обёртка ломала контент markdown-фенсом внутри `<style>`. Сырьём карточка вливается в файл корректно.
- Фикс бага `undefined` в читалке (текстовые ноды: `nodeType===3` → `textContent`).
- Человекоподобный скролл при ожидании (`human_scroll`: переменная величина, изредка откат вверх; интервал 1.5–3.5с).
- Рефакторинг корня в пакеты browser/proxy, entrypoint `run.py`.
- Читалка чистит артефакты рендера: `[Canvas]`-маркер, битый цитатный якорь `*]()`, схлопывание пустых строк.
- **Фикс порчи символов**: инкрементальный стрим-диф (`current_text[i:]` по common-prefix) вставлял лишние символы в токены (`todo_7_744`, `GENERATION_TIMEOU_T`) на ре-рендерах DOM. Теперь `browser_manager` НЕ стримит по кускам — отдаёт полный чистый финальный текст одним `yield` на завершении (парсер всё равно собирает аргументы тулза атомарно по `<<<END>>>`). Стриминг по токенам пожертвован ради стабильности.

### Фикс бесконечного цикла агента (Kilo) 2026-05-27
Симптом: Kilo вечно «ожидает ответа», диалог рос на пару `assistant`+`tool` каждую итерацию (17→27+ сообщений), один и тот же `write index.html` повторялся.
Причина: Kilo агентский — шлёт всю историю; на продолжении новых `user` нет (только `tool`-результаты). Старый delta брал `users[len(stored):]` → пусто → промпт 0 симв → прокси перечитывал тот же экран → тот же ответ → ∞.
Фикс:
- `conversation_store.match`: delta = сообщения **после последнего `assistant`** (непрожёванный хвост: `tool`-результаты / новый `user`).
- `format_delta_prompt`: `role=="tool"` → `[Результат инструмента]: <текст>` (релей результата в браузерный чат).
- `api_server`: guard — если диалог найден и delta пуста, мгновенный `finish_reason:stop` без браузера (страховка от цикла).
- Стриминг возвращён в corruption-safe виде: отдаём префикс, стабильный в ДВУХ подряд чтениях DOM (исключает ре-рендеры), + досыл хвоста на завершении. Скролл `human_scroll` в лупе сохранён.

### Фикс отступов (критично для Python) 2026-05-27
ChatGPT сохраняет отступы/пробелы ТОЛЬКО внутри код-блока (`PRE`). Сырой многострочный код в markdown → ведущие пробелы схлопываются, `__name__`→`name` (подчёркивания съедает emphasis). Решение: контент файла оборачивается в код-фенс (```), модель инструктируется через **Custom Instructions** (актуальный текст — [docs/custom_instructions.txt](docs/custom_instructions.txt), его надо вставить в ChatGPT → Personalization). Читалка извлекает `PRE` сырьём (фенсы не навязывает), `response_parser._strip_fence` страховочно срезает обрамляющий ``` из WRITE-контента и EDIT old/new. Проверено: `def/if/for` с вложенными отступами → VALID PYTHON.

### Фикс выбора модели (Instant/Thinking) 2026-05-27
Не переключалась модель (всегда Instant). Причины: (1) `select_model` искал кнопку по тексту/устаревшему testid — кликал не ту; (2) выбор только на `is_new_chat`; (3) пункты моделей — это `[role="menuitemradio"]`, а селектор ловил лишь `menuitem`/`option`.
Фикс (`browser_manager`):
- Кнопка-переключатель = `button[aria-haspopup="menu"]`, текст которой = текущая модель (`Instant`/`Thinking`/`Auto`). Точно и стабильно (testid у неё нет).
- `select_model` читает текущую модель из текста кнопки и переключает ТОЛЬКО при несовпадении — в обе стороны (в т.ч. `Thinking→Instant`).
- Селектор пунктов меню расширен на `menuitemradio`. Реальные пункты: `Instant`, `Thinking · Standard`, `Configure...`.
- Вызов `select_model(target_model)` перенесён из ветки нового чата — применяется перед КАЖДЫМ сообщением (модель можно менять в любой момент диалога).
Проверено: запрос с `model=thinking` → лог «Модель выбрана: Thinking».

### Проверено end-to-end (через `scripts/tooltest.py`)
Все туллзы отдают чистые нативные `tool_calls`: `glob` (`**/*.py`), `read` (`config.py`), `edit` (old/new точечно, `GENERATION_TIMEOUT 300→600`), `write` (валидный файл). `scripts/tooltest.py` — харнесс: `python scripts/tooltest.py "<задача>"`, шлёт стрим-запрос с набором тулзов, печатает сводку tool_calls. ВАЖНО: слать уникальный текст (дедуп по user-хешам).

### Известные риски / следующее

- Дедуп `conversation_store` по user-хешам: идентичный повторный запрос трактуется как продолжение чата → пустая delta → ничего не вводится. Учитывать при тестах (слать уникальный текст).
- Дампы запросов копятся в `temp/request_dump_*.json` (`api_server.dump_request`). Чистить периодически (`rm temp/request_dump_*.json`).

Фильтры читалки (применены в [browser/cdp_native.py:155-166](browser/cdp_native.py#L155-L166)):

- Хвост `[Canvas]` обрезается до маркера.
- Артефакт `*]()` (цитатные якоря) — стрип `re.sub`.
- Префикс `Thinking…` / `Thought for N seconds` — стрип.
