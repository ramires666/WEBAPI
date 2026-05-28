from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import uvicorn
import json
import asyncio
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from browser_manager import BrowserManager
from conversation_store import ConversationStore

app = FastAPI(title="ChatGPT API Proxy (Human-Mimic Edition)")
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

def format_delta_prompt(delta_messages: List[ChatMessage], tools: Optional[List[Dict[str, Any]]] = None, is_new_chat: bool = False) -> str:
    prompt = ""
    if tools and is_new_chat:
        tools_desc = json.dumps(tools, ensure_ascii=False, indent=2)
        prompt += (
            "[SYSTEM INSTRUCTION]\nТебе доступны следующие инструменты (Tool Use). "
            "Если нужно вызвать инструмент — верни ТОЛЬКО JSON-блок вида "
            '{"tool_call": {"name": "...", "arguments": {...}}}.\n'
            f"Доступные инструменты:\n{tools_desc}\n\n"
        )

    for msg in delta_messages:
        content = msg.content
        if isinstance(content, list):
            content = " ".join([p.get("text", "") for p in content if p.get("type") == "text"])
        
        if msg.role == "system":
            if is_new_chat:
                prompt += f"[SYSTEM]: {content}\n\n"
        elif msg.role == "user":
            prompt += f"{content}\n\n"
        elif msg.role == "assistant":
            prompt += f"[PREVIOUS ASSISTANT REPLY]: {content}\n\n"
            
    return prompt.strip()

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
    
    # Склеиваем только дельту! Если is_new_chat, склеится вся история.
    prompt_text = format_delta_prompt(delta_messages, request.tools, is_new_chat)
    
    if request.stream:
        async def event_generator():
            full_reply = ""
            try:
                # В browser_manager передаем флаг is_new_chat, чтобы он нажал кнопку если надо
                async for chunk in browser_manager.send_prompt_and_stream(prompt_text, target_model, is_new_chat, chat_url):
                    full_reply += chunk
                    response_data = {
                        "id": "chatcmpl-proxy",
                        "object": "chat.completion.chunk",
                        "model": request.model,
                        "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}]
                    }
                    yield f"data: {json.dumps(response_data)}\n\n"
                    
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

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
