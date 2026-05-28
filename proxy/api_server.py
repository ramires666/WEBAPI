from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import uvicorn
import json
import asyncio
import os
import time
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from browser.browser_manager import BrowserManager
from proxy.conversation_store import ConversationStore
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
browser_manager = BrowserManager()

class ChatMessage(BaseModel):
    role: str
    content: str | List[Dict[str, Any]]

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    tools: Optional[List[Dict[str, Any]]] = None

current_state = ConversationStore()

from proxy.prompt_optimizer import optimize_tools, optimize_system_message

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

    final_prompt = prompt.strip()
    logger.info(f"🚀 Сформирован промпт для браузера: {len(final_prompt)} символов (было бы ~40k+ без оптимизации).")
    return final_prompt

@app.on_event("startup")
async def startup_event():
    await browser_manager.start_browser()

@app.on_event("shutdown")
async def shutdown_event():
    await browser_manager.stop_browser()

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

    is_new_chat, delta_messages, chat_url, row_id = current_state.match(request.messages)

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
    
    if request.stream:
        from proxy.response_parser import ResponseParser
        async def event_generator():
            full_reply = ""
            try:
                # В browser_manager передаем флаг is_new_chat, чтобы он нажал кнопку если надо
                chunk_stream = browser_manager.send_prompt_and_stream(prompt_text, target_model, is_new_chat, chat_url)
                
                # Мы всё равно сохраним полный ответ для истории
                parser = ResponseParser()
                async for chunk_data in parser.process_stream(chunk_stream, request.model):
                    # chunk_data - это уже готовая строка вида "data: {...}\n\n"
                    # Но нам нужно сохранить сырой текст (вместе с markdown), чтобы сохранить в history
                    # Это сложно, поэтому просто извлекаем content из json
                    try:
                        json_str = chunk_data.replace("data: ", "").strip()
                        parsed = json.loads(json_str)
                        delta = parsed["choices"][0]["delta"]
                        if "content" in delta:
                            full_reply += delta["content"]
                        elif "tool_calls" in delta:
                            # Сохраняем тул колл в историю как текст, чтобы знать, что мы вызывали
                            func = delta["tool_calls"][0]["function"]
                            full_reply += f'\n```json\n{{"tool_call": {{"name": "{func["name"]}", "arguments": {func["arguments"]}}}}}\n```\n'
                    except:
                        pass
                        
                    yield chunk_data
                    
                _url = await browser_manager.get_current_url()
                current_state.upsert(row_id, request.messages, full_reply, _url)

                final_data = {
                    "id": "chatcmpl-proxy",
                    "object": "chat.completion.chunk",
                    "model": request.model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                }
                yield f"data: {json.dumps(final_data)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                
        return StreamingResponse(event_generator(), media_type="text/event-stream")
    else:
        full_text = ""
        async for chunk in browser_manager.send_prompt_and_stream(prompt_text, target_model, is_new_chat, chat_url):
            full_text += chunk

        _url = await browser_manager.get_current_url()
        current_state.upsert(row_id, request.messages, full_text, _url)

        return {
            "id": "chatcmpl-proxy",
            "object": "chat.completion",
            "model": request.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": full_text}, "finish_reason": "stop"}]
        }
