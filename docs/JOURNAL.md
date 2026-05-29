# Журнал разработки

## 2026-05-29 — Диагностика "тест молчит после Генерация завершена"

### Симптом
- Тесты на 3 профилях: первый отвечает быстро, второй медленно, третий — таймаут.
- В браузере (визуально) ответ с `<<<WRITE>>>…<<<END>>>` блок присутствует — модель отработала.
- Но `scripts/tooltest.py` зависает после лога `[Profile_X] Генерация завершена.` и доходит до `urllib`-таймаута 180с.
- Вывод: проблема НЕ в генерации ChatGPT, а в пайплайне после завершения генерации (читалка DOM → парсер → SSE).

### Что landed (commit b4bb3e4)
Инструментирование `browser/browser_manager.py` и `proxy/api_server.py` для локализации фазы зависания.

**`browser_manager.send_prompt_and_stream`:**
- `t_submit`, `t_first_chunk`, `t_done` через `time.perf_counter()` — точки замера.
- Лог `📝 FINAL_TEXT: len=N, has_write=…, has_end=…, has_edit=…, head=...` перед `yield tail` — проверяем, что делимитер-блок попал в `last_text` ИЗ читалки.
- Лог `📤 yield tail: len=…` — что именно досылается клиенту.
- Тайминги каждой post-фазы: `⏱ click_download_buttons`, `⏱ collect_files`, `⏱ popup`.
- Сводка `📊 ТАЙМИНГ: submit→first / gen / post / total`.
- **Срез паразитных пауз**: `asyncio.sleep(2.0)` после `click_download_buttons` теперь только если `clicked > 0` (раньше был всегда). `sleep(0.5)` → `0.3`.
- **`_handle_personality_popup` обёрнут в `wait_for(timeout=1.0)`** — fail-fast если CDP-сессия зависла на поиске поп-апа.

**`api_server.event_generator`:**
- Счётчики `chunks_yielded` / `tool_call_count` — сколько SSE-чанков и тулзов отдали клиенту.
- Лог `📤 SSE: yielded=N chunks, tool_calls=K` после исчерпания потока парсера.
- `manager.get_current_url()` обёрнут в `wait_for(timeout=3.0)` — если CDP завис, не блокируем `[DONE]`.
- Логи `🔚 СТАРТ post-stream` и `✅ ЗАВЕРШЕНО SSE [DONE]` — видно границы post-stream фазы.

### Диагностический протокол
После рестарта сервера прогон `python scripts/tooltest.py "camo_<uuid> создай …"`. По логам определяем, где провал:

1. **Если нет `📝 FINAL_TEXT`** → `read_last_assistant` не отдал текст, или зашли в ветку без `last_text`.
2. **Если `FINAL_TEXT` есть, но `has_write=False`** → делимитер-блок не дошёл из DOM до читалки (Canvas/PRE проблемы).
3. **Если `📤 SSE: yielded=N, tool_calls=0` и `FINAL_TEXT has_write=True`** → парсер не распознал блок (regex/буфер).
4. **Если `СТАРТ post-stream` есть, но нет `✅ ЗАВЕРШЕНО`** → виснет в `upsert` (sqlite lock) или после `[DONE]`.
5. **Если нет `СТАРТ post-stream`** → виснет в `event_generator`-loop (парсер async-for).
6. **Тайминги показывают**, что (a) генерация в норме (`gen` < 30c), но `post` огромный — значит post-processing виноват; (b) `gen` сам по себе огромный — модель/CDP-фокус виноват.

### Предыдущие незакрытые фронты (контекст)
- Phase 4: расширение тулзов (`list_dir`, `multi_edit`, `delete`, `move`, `mkdir`, `web_fetch`, `todo_write`, `apply_patch`).
- Phase 5: Gemini sub-agents (одна Chrome-инстанс, 2 вкладки, subagent_dispatcher).
- Чистка `chrome_work_profile/` (осиротевший каталог).
- End-to-end тест авто-summary с уменьшенным `TOKEN_LIMIT`.
- KICKSTART system-prompt-fallback на случай профилей без Custom Instructions.

### Принятые архитектурные решения (для истории)
- **Делимитер-протокол** `<<<VERB>>>…<<<END>>>` вместо JSON — победил эскейп многострочного контента.
- **Custom Instructions** аккаунта ChatGPT (Settings → Personalization) — единственное надёжное место для протокола; in-chat system prompt модель игнорирует/портит. Текст в `docs/custom_instructions.txt`.
- **Стриминг по префиксу, стабильному в ДВУХ подряд чтениях DOM** — компромисс между live-токенами и corruption-safety при ре-рендерах.
- **Pool round-robin + sticky binding** по `browser_id` в `conversation_store` — каждый диалог намертво привязан к своему профилю Chrome.
- **`button[aria-haspopup="menu"]`** + substring-match `instant/thinking/auto` — единственно стабильный селектор переключателя модели (testid отсутствует, текст вида `"GPT-5 Instant"`).
- **`Emulation.setFocusEmulationEnabled` + периодический refresh каждые 5с** — анти-throttle для фоновых вкладок (без этого ChatGPT медленнее печатает в неактивной вкладке).

### Метрика "что должно быть"
- Instant модель: `gen` ≈ 3–15с на короткие задачи, `post` < 2с, `total` < 20с.
- Thinking: `gen` 20–120с, остальное так же.
- `chunks_yielded` ≥ 1 для не-пустых ответов; `tool_calls` = 1 для тестовых WRITE-запросов.
