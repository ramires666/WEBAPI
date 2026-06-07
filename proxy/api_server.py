from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import uvicorn
import json
import asyncio
import os
import time
import glob
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from browser.browser_pool import BrowserPool
from proxy.conversation_store import ConversationStore, client_fingerprint
from proxy.token_counter import estimate_messages_tokens
from proxy.context_summarizer import build_summary_prompt, save_summary, build_context_injection
from config import AUTO_SUMMARY_ENABLED, TOKEN_LIMIT, PROFILES, PROFILES_DIR, WORK_DIR, DUMP_MAX_AGE_DAYS
from loguru import logger

app = FastAPI(title="ChatGPT API Proxy (Human-Mimic Edition)")

DUMP_DIR = r"W:\_python\APIPROXY\temp"
os.makedirs(DUMP_DIR, exist_ok=True)

def dump_request(data: dict):
    """Дампит входящий запрос для анализа system prompt."""
    try:
        ts = int(time.time())
        dump_path = os.path.join(DUMP_DIR, f"request_dump_{ts}.json")
        with open(dump_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        total_chars = sum(len(str(m.get("content", ""))) for m in data.get("messages", []))
        tools_chars = len(json.dumps(data.get("tools", []), ensure_ascii=False))
        logger.info(
            "📥 DUMP: {} | messages={} | content≈{}tok | tools≈{}tok | file={}",
            data.get("model", "?"),
            len(data.get("messages", [])),
            total_chars // 4,
            tools_chars // 4,
            dump_path,
        )
    except Exception as e:
        logger.warning("Dump failed: {}", e)

def cleanup_old_temp(max_age_days: int = DUMP_MAX_AGE_DAYS) -> int:
    """Удаляет дамп-скрэтч в temp/ старше max_age_days (по mtime). Возвращает число удалённых."""
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for pattern in ("request_dump_*.json", "timeout_dump_*.html"):
        for path in glob.glob(os.path.join(DUMP_DIR, pattern)):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    if removed:
        logger.info("🧹 temp cleanup: удалено {} файлов старше {}д", removed, max_age_days)
    return removed

browser_pool = BrowserPool(PROFILES, PROFILES_DIR, WORK_DIR)

class ChatMessage(BaseModel):
    role: str
    content: str | List[Dict[str, Any]]

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    tools: Optional[List[Dict[str, Any]]] = None

current_state = ConversationStore()

from proxy.prompt_optimizer import optimize_tools, optimize_system_message, PROXY_REMINDER

def format_delta_prompt(delta_messages: List[ChatMessage], tools: Optional[List[Dict[str, Any]]] = None, is_new_chat: bool = False) -> str:
    prompt = ""

    for msg in delta_messages:
        content = msg.content
        if isinstance(content, list):
            content = " ".join([p.get("text", "") for p in content if p.get("type") == "text"])
            
        import re
        content = re.sub(r'<environment_details>.*?</environment_details>', '', content, flags=re.DOTALL).strip()
        
        if msg.role == "system":
            if is_new_chat:
                optimized_content = optimize_system_message(content)
                if optimized_content:
                    prompt += f"[SYSTEM]: {optimized_content}\n\n"
        elif msg.role == "user":
            prompt += f"{content}\n\n"
        elif msg.role == "assistant":
            prompt += f"[PREVIOUS ASSISTANT REPLY]: {content}\n\n"
        elif msg.role == "tool":
            prompt += f"[Результат инструмента]: {content}\n\n"

    final_prompt = prompt.strip() + PROXY_REMINDER
    logger.info(f"🚀 Сформирован промпт для браузера: {len(final_prompt)} символов (было бы ~40k+ без оптимизации).")
    return final_prompt

@app.on_event("startup")
async def startup_event():
    cleanup_old_temp()

    async def _temp_cleanup_loop():
        while True:
            await asyncio.sleep(24 * 3600)
            try:
                cleanup_old_temp()
            except Exception as e:
                logger.warning("temp cleanup loop failed: {}", e)

    asyncio.create_task(_temp_cleanup_loop())
    await browser_pool.start_all()

@app.on_event("shutdown")
async def shutdown_event():
    await browser_pool.stop_all()

@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": "Instant", "object": "model", "created": 1715367049, "owned_by": "system"},
            {"id": "Thinking", "object": "model", "created": 1715367049, "owned_by": "system"}
        ]
    }

TITLE_MARKERS = (
    "you are a title generator",
    "generate a brief title",
    "generate a title for this conversation",
    "thread title",
)


def _content_text(content) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return content or ""


def is_title_request(messages: List[ChatMessage]) -> bool:
    for m in messages:
        if any(mark in _content_text(m.content).lower() for mark in TITLE_MARKERS):
            return True
    return False


def make_local_title(messages: List[ChatMessage]) -> str:
    user_txt = ""
    for m in messages:
        if m.role == "user":
            user_txt = _content_text(m.content)
    lines = [ln.strip() for ln in user_txt.splitlines() if ln.strip()]
    base = lines[-1] if lines else "Беседа"
    title = " ".join(base.split()[:6])[:50].strip() or "Беседа"
    return title[:1].upper() + title[1:]


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    # Дампим полный запрос для анализа
    dump_request(request.model_dump())
    
    target_model = "Thinking" if "thinking" in request.model.lower() else "Instant"

    # Запрос генерации заголовка от клиента — отвечаем локально, БЕЗ браузера/чата
    if is_title_request(request.messages):
        title = make_local_title(request.messages)
        if request.stream:
            async def title_gen():
                chunk = {"id": "chatcmpl-title", "object": "chat.completion.chunk", "model": request.model,
                         "choices": [{"index": 0, "delta": {"content": title}, "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n"
                done = {"id": "chatcmpl-title", "object": "chat.completion.chunk", "model": request.model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                yield f"data: {json.dumps(done)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(title_gen(), media_type="text/event-stream")
        return {
            "id": "chatcmpl-title", "object": "chat.completion", "model": request.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": title}, "finish_reason": "stop"}],
        }

    is_new_chat, delta_messages, chat_url, row_id, browser_id = current_state.match(request.messages)

    # --- Выбор браузера ---
    manager = browser_pool.get(browser_id) if not is_new_chat else None
    if manager is None:
        browser_id, manager = browser_pool.next_for_new_chat()
        if not is_new_chat:
            is_new_chat = True
            chat_url = None
            row_id = None

    # --- Подсчёт токенов и авто-саммари ---
    token_count = estimate_messages_tokens(request.messages)
    logger.info("📊 Токенов в запросе: ~{} (лимит: {})", token_count, TOKEN_LIMIT)

    if AUTO_SUMMARY_ENABLED and token_count > TOKEN_LIMIT and not is_new_chat:
        logger.warning("⚠️ Превышен лимит токенов ({} > {}). Генерируем саммари...",
                       token_count, TOKEN_LIMIT)
        try:
            summary_prompt = build_summary_prompt()
            summary_text = ""
            async for chunk in manager.send_prompt_and_stream(
                summary_prompt, target_model, False, chat_url
            ):
                summary_text += chunk
            cid = client_fingerprint(request.messages)
            summary_path = save_summary(summary_text, cid)
            logger.info("📋 Саммари готово ({}), переключаемся на новый чат", summary_path)
            # Переключаемся на новый чат
            browser_id, manager = browser_pool.next_for_new_chat()
            is_new_chat = True
            chat_url = None
            row_id = None
            context_prefix = build_context_injection(summary_text)
        except Exception as e:
            logger.error("❌ Ошибка генерации саммари: {}. Продолжаем без саммари.", e)
            context_prefix = ""
    else:
        context_prefix = ""

    # Нечего отправлять (история заканчивается нашим же ответом) — не трогаем
    # браузер и сразу завершаем, чтобы не зациклить агента на перечитывании экрана.
    if not is_new_chat and not delta_messages:
        if request.stream:
            async def _empty_gen():
                stop = {"id": "chatcmpl-proxy", "object": "chat.completion.chunk",
                        "model": request.model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                yield f"data: {json.dumps(stop)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(_empty_gen(), media_type="text/event-stream")
        return {"id": "chatcmpl-proxy", "object": "chat.completion", "model": request.model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}]}

    # Склеиваем только дельту! Если is_new_chat, склеится вся история.
    prompt_text = format_delta_prompt(delta_messages, request.tools, is_new_chat)
    if context_prefix:
        prompt_text = context_prefix + prompt_text

    # Смысловой текст последнего user-сообщения — его ВПЕЧАТАЕМ (человеко-ввод);
    # систему/контекст/результаты тулзов — вставим пастой (insert_text).
    typed_segment = ""
    import re as _re_ts
    for _m in delta_messages:
        if _m.role == "user":
            _c = _m.content
            if isinstance(_c, list):
                _c = " ".join(p.get("text", "") for p in _c if isinstance(p, dict) and p.get("type") == "text")
            _c = _re_ts.sub(r'<environment_details>.*?</environment_details>', '', _c or "", flags=_re_ts.DOTALL).strip()
            if _c:
                typed_segment = _c

    if request.stream:
        from proxy.response_parser import ResponseParser
        async def event_generator():
            full_reply = ""
            chunks_yielded = 0
            tool_call_count = 0
            tool_acc = {}  # index -> {"name", "args"} для сборки stream tool_calls
            try:
                # Используем выбранный браузер
                chunk_stream = manager.send_prompt_and_stream(prompt_text, target_model, is_new_chat, chat_url, typed_segment=typed_segment)
                parser = ResponseParser()

                # ── Producer/consumer: пульс БЕЗ отмены генератора. ──────────────
                # КРИТИЧНО: НЕЛЬЗЯ оборачивать parser.__anext__() в asyncio.wait_for —
                # таймаут wait_for ОТМЕНЯЕТ корутину __anext__, бросая CancelledError
                # ВНУТРЬ async-генератора браузера (page.get/sleep), что убивает всю
                # отправку (генератор закрывается → следующий __anext__ = StopAsyncIteration
                # → 0 чанков, в браузер ничего не печатается). Поэтому генератор крутится
                # в отдельной задаче-producer и кладёт чанки в очередь, а consumer тут
                # тянет из очереди с таймаутом 2.5с ради ПУЛЬСА — producer при этом жив.
                queue = asyncio.Queue()
                _SENTINEL = object()
                _producer_exc = {}

                async def _producer():
                    try:
                        async for _cd in parser.process_stream(chunk_stream, request.model):
                            await queue.put(_cd)
                    except Exception as _pe:
                        _producer_exc["e"] = _pe
                    finally:
                        await queue.put(_SENTINEL)

                _prod_task = asyncio.create_task(_producer())
                _hb_start = time.perf_counter()
                _hb_last_status = ""
                _hb_emitted = False
                _hb_dots = 0
                try:
                    while True:
                        try:
                            chunk_data = await asyncio.wait_for(queue.get(), timeout=2.5)
                        except asyncio.TimeoutError:
                            # Генерация идёт — показываем ЖИВОЙ статус (что модель пишет/
                            # делает прямо сейчас), вытащенный из потока браузером. Между
                            # сменами статуса — точки через пробел, с секундами в скобках.
                            # Producer НЕ трогаем. В full_reply пульс НЕ добавляем.
                            _elapsed = int(time.perf_counter() - _hb_start)
                            try:
                                _status = manager.get_live_status().strip()
                            except Exception:
                                _status = ""
                            if not _status:
                                _status = "⏳ генерирую ответ"
                            if _status != _hb_last_status:
                                _hb_last_status = _status
                                _hb_dots = 0
                                _hb_txt = ("\n" if _hb_emitted else "") + f"{_status} ({_elapsed}с)"
                                _hb_emitted = True
                            else:
                                _hb_dots += 1
                                _hb_txt = f" ({_elapsed}с)" if _hb_dots % 8 == 0 else " ·"
                            _hb = {
                                "id": "chatcmpl-proxy",
                                "object": "chat.completion.chunk",
                                "model": request.model,
                                "choices": [{"index": 0, "delta": {"content": _hb_txt}, "finish_reason": None}],
                            }
                            yield f"data: {json.dumps(_hb)}\n\n"
                            continue
                        if chunk_data is _SENTINEL:
                            break
                        # ── real chunk: keep the EXISTING parse/accumulate logic verbatim ──
                        try:
                            json_str = chunk_data.replace("data: ", "").strip()
                            parsed = json.loads(json_str)
                            delta = parsed["choices"][0]["delta"]
                            if "content" in delta:
                                full_reply += delta["content"]
                            elif "tool_calls" in delta:
                                # Стримовый tool_call: name+id только в первом чанке,
                                # аргументы докапливаются по index в последующих.
                                tc = delta["tool_calls"][0]
                                idx = tc.get("index", 0)
                                func = tc.get("function", {})
                                if func.get("name"):
                                    tool_acc[idx] = {"name": func["name"], "args": func.get("arguments", "")}
                                    tool_call_count += 1
                                elif idx in tool_acc:
                                    tool_acc[idx]["args"] += func.get("arguments", "")
                        except:
                            pass
                        chunks_yielded += 1
                        yield chunk_data
                finally:
                    # Клиент отвалился / выходим — гасим producer (и, как следствие,
                    # генератор браузера), чтобы он не висел в фоне.
                    if not _prod_task.done():
                        _prod_task.cancel()
                    try:
                        await _prod_task
                    except BaseException:
                        pass

                if _producer_exc.get("e"):
                    raise _producer_exc["e"]

                # Дособираем stream tool_calls в текст истории (после полного стрима)
                for _idx in sorted(tool_acc):
                    _tc = tool_acc[_idx]
                    full_reply += f'\n```json\n{{"tool_call": {{"name": "{_tc["name"]}", "arguments": {_tc["args"]}}}}}\n```\n'
                logger.info("📤 SSE: yielded={} chunks, tool_calls={}", chunks_yielded, tool_call_count)
                logger.info("🔚 СТАРТ post-stream (get_current_url + upsert)")
                _t = time.perf_counter()
                try:
                    _url = await asyncio.wait_for(manager.get_current_url(), timeout=3.0)
                except asyncio.TimeoutError:
                    logger.warning("get_current_url таймаут 3s — используем пустую URL")
                    _url = ""
                logger.info("✅ get_current_url: {:.2f}s", time.perf_counter() - _t)
                current_state.upsert(row_id, request.messages, full_reply, _url, token_count, browser_id=browser_id)

                final_data = {
                    "id": "chatcmpl-proxy",
                    "object": "chat.completion.chunk",
                    "model": request.model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool_call_count > 0 else "stop"}]
                }
                yield f"data: {json.dumps(final_data)}\n\n"
                yield "data: [DONE]\n\n"
                logger.info("✅ ЗАВЕРШЕНО SSE [DONE]")
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")
    else:
        full_text = ""
        async for chunk in manager.send_prompt_and_stream(prompt_text, target_model, is_new_chat, chat_url, typed_segment=typed_segment):
            full_text += chunk

        _url = await manager.get_current_url()
        current_state.upsert(row_id, request.messages, full_text, _url, token_count, browser_id=browser_id)

        return {
            "id": "chatcmpl-proxy",
            "object": "chat.completion",
            "model": request.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": full_text}, "finish_reason": "stop"}]
        }
