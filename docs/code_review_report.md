# Code Review Report

Дата: 2026-05-31

Область анализа: `run.py`, `config.py`, `browser/`, `proxy/`, `scripts/`, `tests/`, `README.md`, `docs/`.

Изменения кода не выполнялись. Создан только этот отчет.

## Критичные / High

1. `browser/browser_manager.py:535-551`, `browser/browser_manager.py:641-679` — короткий ответ может быть потерян полностью.
   Baseline последнего assistant-сообщения снимается после submit. Если ChatGPT успел ответить до чтения baseline, новый ответ принимается за старый, `final_text` становится пустым.
   Рекомендация: снимать baseline до отправки запроса; observer ставить до submit или хранить количество/id assistant-сообщений до отправки.

2. `proxy/response_parser.py:221-247` — незакрытый `<<<WRITE ...>>>` на финальном flush превращается в валидный `write` tool_call.
   При timeout/обрыве генерации клиент может записать усеченный файл.
   Рекомендация: если нет `<<<END>>>`, не эмитить tool_call; вернуть ошибку/обычный content и завершить стрим безопасно.

3. `browser/browser_manager.py:131-134`, `browser/browser_manager.py:707-718`, `browser/file_extractor.py:37-55` — общий `TEMP_DOWNLOADS` для всех профилей.
   При параллельной работе один профиль может прочитать и удалить файл, скачанный другим профилем.
   Рекомендация: делать download-dir на профиль/запрос или вводить lock и привязку файлов к request id.

4. `proxy/api_server.py:62-70` — схема `ChatMessage` не совместима с частью OpenAI-compatible сообщений.
   `content` обязателен и не допускает `None`; assistant/tool messages часто приходят как `content: null` или assistant только с `tool_calls`. Это может дать 422 на валидном запросе клиента.
   Рекомендация: разрешить `content: ... | None = None`, добавить поля `tool_calls`, `tool_call_id`, `name` или разрешить extra-поля.

5. `proxy/api_server.py:325-327` — ошибка внутри SSE отдается как `data: {"error": ...}` без OpenAI `choices` и без `[DONE]`.
   Клиент может зависнуть или сломать парсинг стрима.
   Рекомендация: логировать исключение и отдавать валидный финальный SSE chunk + `[DONE]`, либо возвращать HTTP error до начала стрима.

6. `run.py:34` — сервер слушает `0.0.0.0`, хотя проект описан как локальный прокси.
   Без авторизации это открывает управление браузерным ChatGPT для сети, если порт доступен извне.
   Рекомендация: по умолчанию bind на `127.0.0.1`; внешний host включать только через env/config.

7. `README.md:38`, `README.md:43`, `README.md:55` — README ведет на старый `main.py` и старую архитектуру.
   Актуальная точка входа — `run.py`, структура уже `browser/`, `proxy/`, `profiles/`, `work/`.
   Рекомендация: переписать quickstart и описание файлов под текущую архитектуру.

## Средние / Medium

1. `proxy/conversation_store.py:96-148` — диалоги матчятся только по user-сообщениям и IDE-маркеру.
   Одинаковые первые запросы из одного IDE-клиента могут приклеиться к чужому старому ChatGPT-чату, особенно между проектами.
   Рекомендация: включить в namespace стабильный workspace/root/project id; не вырезать весь cwd-контекст из сигнатуры.

2. `proxy/api_server.py:192-199` — при потере sticky browser продолжение переводится в новый чат, но `delta_messages` не пересобирается.
   Новый браузер получит только хвост после последнего assistant, без полной истории или summary.
   Рекомендация: при fallback в новый чат отправлять всю историю, summary или явно сбрасывать матчинг.

3. `proxy/api_server.py:204-224`, `proxy/conversation_store.py:78-80` — auto-summary может повторяться и загрязняет рабочий чат.
   История клиента не компактизируется, поле `summarized` не используется, summary prompt отправляется в текущий ChatGPT-чат.
   Рекомендация: хранить состояние summary/compact-hash; генерировать summary в отдельном временном чате или локально.

4. `proxy/api_server.py:329-342` — non-streaming режим не проходит через `ResponseParser`.
   Delimiter tool blocks вернутся сырым текстом вместо native `tool_calls`.
   Рекомендация: либо не поддерживать `stream=false`, либо парсить полный ответ тем же parser и формировать OpenAI-compatible response.

5. `proxy/api_server.py:23-29` — dump-файлы именуются секундным timestamp.
   Параллельные запросы в одну секунду могут перезаписать дамп.
   Рекомендация: добавить миллисекунды, pid или uuid.

6. `proxy/api_server.py:166-167` — каждый запрос полностью пишется на диск.
   Дампы могут содержать system prompt, tool outputs, пути, фрагменты файлов и другие чувствительные данные.
   Рекомендация: включать dump через env-флаг, маскировать секреты, уменьшить retention или писать только метаданные по умолчанию.

7. `proxy/response_parser.py:9-10`, `proxy/response_parser.py:156-158` — парсер атрибутов хрупкий.
   `_HEADER_RE` ломается на `>` внутри значения, `_ATTR_RE` не поддерживает кавычки внутри attr value. Regex/path вроде `pattern="a>b"` может распарситься неверно.
   Рекомендация: парсить header посимвольно до `>>>` с учетом quoted strings и escaping.

8. `browser/cdp_native.py:218-221`, `proxy/response_parser.py:419-422` — любое текстовое `[Canvas]` обрезает хвост ответа.
   Если литерал `[Canvas]` нужен внутри файла или документации, контент будет поврежден.
   Рекомендация: фильтровать Canvas по DOM-структуре/классам, а не по голому текстовому маркеру.

9. `browser/file_extractor.py:45-55` — скачанные файлы читаются как UTF-8 и удаляются в `finally`.
   Бинарные или не-текстовые вложения будут испорчены в выводе и удалены без восстановления.
   Рекомендация: определять тип/размер; бинарные не удалять или отдавать как base64/metadata.

10. `browser/cdp_native.py:267-273` — stream observer не переустанавливается при повторном вызове.
    При SPA-навигации старый observer может остаться на detached root, а `window.__pxFull` перестанет обновляться.
    Рекомендация: каждый запрос disconnect старый observer и observe текущий `main`/`body`.

11. `proxy/prompt_optimizer.py:4-8`, `proxy/response_parser.py:331-372` — список разрешенных tools не совпадает с реально эмитимыми tool_calls.
    `execute_command` и `background_process` разрешены в optimizer, но parser их не генерирует; BG-family отключен.
    Рекомендация: оставить только реальные tool names или добавить явный mapping.

12. `scripts/tooltest.py:16-65` — synthetic harness противоречит текущему правилу проверки реальным Kilo dump.
    Такой тест может зеленеть, когда живой Kilo ломается.
    Рекомендация: пометить deprecated или заменить wrapper'ом вокруг `scripts/replay.py`.

13. `scripts/bgtest.py:36-80` — тест использует отключенные `<<<BG>>>`, `BGSTATUS`, `BGTAIL`, `BGSTOP`.
    Parser прямо помечает BG-family как disabled.
    Рекомендация: переписать под BASH background convention или удалить.

## Низкие / Low

1. `config.py:10`, `config.py:16`, `config.py:21-22`, `proxy/api_server.py:20`, `proxy/conversation_store.py:10` — много абсолютных путей `W:\_python\APIPROXY`.
   Проект сложнее переносить и тестировать.
   Рекомендация: вычислять root через `Path(__file__).resolve()` и давать env overrides.

2. `config.py:6-7` — `ORIGINAL_CHROME_USER_DATA` и `CHROME_PROFILE_NAME` сейчас не используются в актуальном коде.
   При отсутствии `LOCALAPPDATA` импорт `config.py` упадет, хотя константа не нужна.
   Рекомендация: удалить dead config или лениво вычислять только там, где используется.

3. `proxy/prompt_optimizer.py:10-55` — `OPTIMIZED_SYSTEM_PROMPT` не используется и противоречит актуальному fence-правилу.
   В нем написано не оборачивать content в fences, а `docs/custom_instructions.txt` требует fences для сохранения отступов.
   Рекомендация: удалить stale constant или синхронизировать с текущим протоколом.

4. `proxy/api_server.py:74`, `proxy/prompt_optimizer.py:59-85` — `optimize_tools` импортируется, но не вызывается.
   Параметр `tools` в `format_delta_prompt` тоже фактически игнорируется.
   Рекомендация: удалить dead code или реально использовать для диагностики/сжатия prompt.

5. `proxy/context_summarizer.py:94-107` — `get_latest_summary()` не используется.
   Summary сохраняются, но не подхватываются при старте или восстановлении.
   Рекомендация: удалить функцию или интегрировать recovery path.

6. `browser/browser_manager.py:636-637`, `browser/browser_manager.py:665-667` — диагностические дампы пишутся в относительный `temp/`, `final_generation_dump.txt` общий для всех профилей.
   Параллельные запросы могут перезаписывать файл.
   Рекомендация: использовать configured temp dir и включать profile/request/timestamp в имя.

7. `browser/cdp_native.py:75-84`, `browser/browser_manager.py:18-20` — `human_scroll` импортируется, но не используется.
   В `browser_manager.py` есть отдельная inline-логика скролла.
   Рекомендация: удалить helper/import или использовать единый helper.

8. `proxy/response_parser.py:423` — `_content()` безусловно удаляет все `<<<END>>>` из обычного текста.
   Если маркер нужен как литерал, данные потеряются.
   Рекомендация: удалять marker только внутри распознанного tool-блока.

9. `scripts/check_hashes.py:12-13` — захардкожены конкретные `temp/request_dump_1779860*.json`.
   Скрипт не переиспользуем и падает без этих файлов.
   Рекомендация: принимать пути аргументами или искать последние dump-файлы.

10. `tests/test_model_selection.py:21-23` — тестовая конфигурация указывает старый `chrome_work_profile`.
    Основной код уже использует `profiles/` и `work/`.
    Рекомендация: перевести тест на общий `config.py` или явно пометить legacy.

11. `docs/implementation_plan.md:15-53`, `docs/ТЗ.md`, `docs/JOURNAL.md` — часть документации историческая и расходится с кодом.
    Есть старые пути в корне, старое описание hash logic и устаревшие команды.
    Рекомендация: пометить документы как historical или обновить ссылки/команды.

## Приоритет исправлений

1. Сначала: baseline до submit, запрет усеченных `WRITE`, изоляция `TEMP_DOWNLOADS`, валидный SSE error path.
2. Затем: namespace диалогов по workspace, fallback при потере browser_id, non-stream parser, обновление README.
3. После: чистка dead/stale кода, абсолютных путей, устаревших тестов и исторической документации.
