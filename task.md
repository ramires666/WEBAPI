# Задачи: улучшения ChatGPT API Proxy

## Компонент 1: Исправление форматирования ответов

- [x] `cdp_native.py` — стрип "Thinking" prefix из JS + Python
- [x] `cdp_native.py` — полный стрип Canvas-хвоста (маркер + дубль кода)
- [x] `response_parser.py` — стрип артефактов в `_content()`, фильтр пустых чанков
- [x] `response_parser.py` — фенс-стрип для BASH

## Компонент 2: Поддержка терминальных команд

- [x] `prompt_optimizer.py` — расширить ALLOWED_TOOLS (добавлены `execute_command`, `background_process`)

## Компонент 3: Подсчёт токенов и авто-саммари

- [x] `.env` — создан (на диске, в .gitignore)
- [x] `requirements.txt` — добавлен python-dotenv (+ fastapi, uvicorn явно)
- [x] `config.py` — загрузка .env, константы AUTO_SUMMARY_ENABLED / TOKEN_LIMIT / SUMMARY_DIR / DUMP_MAX_AGE_DAYS
- [x] `proxy/token_counter.py` — новый модуль (cyr/latin heuristic, ±15-20%)
- [x] `proxy/context_summarizer.py` — новый модуль (промпт + save/load summary, инъекция в новый чат)
- [x] `proxy/api_server.py` — интеграция: оценка токенов на каждый запрос, авто-саммари при превышении
- [x] `proxy/conversation_store.py` — поля token_count, summarized + миграция ALTER TABLE

## Верификация

- [x] Тест token_counter — 400 латиницы → 100т, 400 кириллицы → 200т, mixed msgs → ok
- [x] Тест загрузки .env / config — все 4 константы читаются, override через .env работает
- [x] Импорт api_server — без ошибок, ConversationStore миграция применяется чисто
- [x] `ensure_summary_dir()` — создаёт `temp/summaries/`

## Остаточные риски (не блокируют, на следующую сессию)

- [ ] Ротация дампов `temp/request_dump_*.json` (есть `DUMP_MAX_AGE_DAYS=7`, но cleanup не реализован)
- [ ] Тест авто-саммари end-to-end (на проде: дождаться диалога ≥200k токенов)
- [ ] Логирование в UTF-8 (cp866-проблема на Windows из walkthrough)
- [ ] Коммит текущих изменений (8 modified + 4 untracked)
