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

### 2026-05-29 — Лог-захват для Claude
- `scripts/run_with_logs.ps1` / `.bat` — запуск `python -u run.py` с `Tee-Object` в `logs/server_<ts>.log`.
- `logs/latest.log` — hardlink на текущий лог (если NTFS не дал — копия после остановки). Фикс-путь для Claude.Read.
- `.gitignore`: добавлен `logs/`.
- Использование (вместо `python run.py`): `powershell -ExecutionPolicy Bypass -File scripts\run_with_logs.ps1`
- Tee-обёртка `scripts/run_tee.py` ставит `PYTHONIOENCODING=utf-8` для дочернего — иначе кириллица в логе превращается в кракозябры (cp866 default консоли).

### 2026-05-29 — Фикс лага 83с (scroll_bottom)
- Между `📝 FINAL_TEXT` и `⏱ click_download_buttons` зависало на 80+ секунд.
- Виновник: `await scroll_bottom(self.page)` в `browser_manager.send_prompt_and_stream` — CDP `synthesize_scroll_gesture` на финальном DOM может стоять минуту.
- Фикс: обёрнут в `asyncio.wait_for(timeout=2.0)`, добавлен лог `⏱ scroll_bottom: Xs`.
- Результат: post=84.5s → 3.4s. scroll_bottom валит таймаут 2с, click/collect/popup отрабатывают мгновенно.
- Открытый вопрос: `gen=130s` (раньше было 16с) — корреляция с минимизированным окном; см. след. блок.

### Правило: тестирование = 3 параллели
- **Запрещено** проверять прокси одним `tooltest.py`. Минимум — 3 параллельных запроса с уникальными промптами.
- Single-shot прячет: дисбаланс между профилями, race в `conversation_store`, throttle одного из браузеров, sticky-binding баги.
- В каждом параллельном ране — сразу логировать `📊 ТАЙМИНГ` по каждому профилю и сравнивать `gen/post/total`. Регрессия = большой разлёт между профилями или один молчит.

### Параметр: NO_MINIMIZE_PROFILES (config.py)
- Профили из этого списка НЕ сворачиваются на старте — нужны для визуального контроля при тестах.
- Default: `Profile_Fixed` остаётся видимым, остальные сворачиваются.
- Override через env: `NO_MINIMIZE_PROFILES="Profile 2,Profile_Fixed"`.

### 2026-05-29 — Окно off-screen вместо MINIMIZED
- MINIMIZED окно у nodriver/CDP ломает рендеринг: пункты меню моделей не рисуются, ввод/`select_model` глохнут.
- Решение: после старта вкладки получаем `Browser.getWindowForTarget` → `Browser.setWindowBounds` с `left=-32000, top=-32000, NORMAL`. DOM живой, CDP работает, экран не светит.
- Профили из `NO_MINIMIZE_PROFILES` остаются на видимой позиции.

### 2026-05-29 — Polling + 3-стадийный селектор + Atlas-UI (Стратегия 4)
Закрыта реliability-проблема `select_model` под разные аккаунт-state'ы ChatGPT.

**Проблемы, которые закрывали по очереди:**
1. На свежей вкладке `aria-haspopup="menu"` кнопка ещё не отрендерилась → раньше один заход возвращал «не найдено». Фикс: polling 8s/0.3s.
2. Atlas-аккаунт (Profile 2) рендерит UI **иначе**: переключатель режима назван `Extended` (вместо `Instant/Thinking/Auto`), под composer'ом preset-плитки `Create an image`/`Write or edit`. Стандартный селектор по тексту не находил.

**Стратегии поиска кнопки-переключателя (в порядке fallback):**
- **(1)** `button[aria-haspopup="menu"]` с текстом, содержащим `instant`/`thinking`/`auto`.
- **(2)** `aria-haspopup="menu"` + `aria-label`/`data-testid` содержит `model`/`модел`.
- **(3)** `[data-testid*="model"]`/`[aria-label*="model"]` с фильтром по `switcher`/`select model`/`testid^=model-`.
- **(4)** Atlas-fallback: любая `aria-haspopup="menu"` кнопка с непустым текстом, исключая sidebar/history-item/composer-plus/send/dictation (whitelist по text/aria-label/data-testid). На Atlas-UI это `Extended`.

**Диагностика при провале всех стратегий:** дамп URL, `document.title`, и таблица первых 30 кнопок (`aria-label` / `data-testid` / `aria-haspopup` / text). Без этого Profile 2 был «чёрной коробкой».

**Косвенная верификация (Atlas-UI):** после клика пункта меню обычная верификация ищет кнопку с текстом `instant/thinking/auto`. На Atlas-аккаунте кнопка после клика всё равно показывает `Extended` (статичный лейбл) → раньше падало в `⚠️ ВЕРИФИКАЦИЯ ПРОВАЛЕНА`. Фикс: если прямая верификация не нашла target, но `click_element` вернул true И в тексте кликнутого пункта был target_low — лог `✅ ВЕРИФИКАЦИЯ (косвенная, Atlas-UI)`. Также расширил ключевые слова поиска кнопки до `instant/thinking/auto/extended/standard`.

**Проверено 3-параллельным тестом с `model=Thinking`:**
- Profile 4 / Profile_Fixed — стратегия `haspopup+text`, прямая верификация ✅.
- Profile 2 — стратегия `atlas-mode-btn(Extended)`, меню `['Instant', 'Thinking • Extended', '', 'Configure...']`, клик `Thinking • Extended`, косвенная верификация ✅, отправка в режиме `Extended` (target='Thinking').

### Метрика "что должно быть"
- Instant модель: `gen` ≈ 3–15с на короткие задачи, `post` < 2с, `total` < 20с.
- Thinking: `gen` 20–120с, остальное так же.
- `chunks_yielded` ≥ 1 для не-пустых ответов; `tool_calls` = 1 для тестовых WRITE-запросов.
