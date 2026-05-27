from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import uvicorn
import json
import asyncio
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from browser_manager import BrowserManager
import hashlib

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

# Храним состояние текущего чата в памяти
class ChatState:
    def __init__(self):
        self.history: List[dict] = []
        self.chat_urls: Dict[str, str] = {}

    def conv_hash(self, messages: List["ChatMessage"]) -> str:
        joined = "|".join(self.hash_message(m) for m in messages)
        return hashlib.md5(joined.encode("utf-8")).hexdigest()

    def hash_message(self, msg: ChatMessage) -> str:
        # Упрощенное хеширование для сравнения
        content = msg.content
        if isinstance(content, list):
            content = " ".join([p.get("text", "") for p in content if p.get("type") == "text"])
        return hashlib.md5(f"{msg.role}:{content}".encode('utf-8')).hexdigest()

    def get_delta_or_diverge(self, new_messages: List[ChatMessage]) -> tuple[bool, List[ChatMessage]]:
        """
        Сравнивает новую историю с текущей.
        Возвращает (is_new_chat, delta_messages)
        """
        if len(new_messages) < len(self.history):
            return True, new_messages # История короче, значит ветвление -> новый чат
            
        # Проверяем, совпадают ли старые сообщения
        for i, old_msg_hash in enumerate(self.history):
            if self.hash_message(new_messages[i]) != old_msg_hash:
                return True, new_messages # Ветвление в середине истории -> новый чат
                
        # Если дошли сюда, значит это идеальное продолжение текущего чата!
        delta = new_messages[len(self.history):]
        return False, delta

    def update_history(self, new_messages: List[ChatMessage], is_new_chat: bool, assistant_reply: str):
        if is_new_chat:
            self.history = [self.hash_message(m) for m in new_messages]
        else:
            for m in new_messages[len(self.history):]:
                self.history.append(self.hash_message(m))
        
        # Добавляем ответ ассистента в нашу историю
        fake_msg = ChatMessage(role="assistant", content=assistant_reply)
        self.history.append(self.hash_message(fake_msg))

current_state = ChatState()

def format_delta_prompt(delta_messages: List[ChatMessage], tools: Optional[List[Dict[str, Any]]] = None) -> str:
    prompt = ""
    if tools:
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
            {"id": "gpt-4o", "object": "model", "created": 1715367049, "owned_by": "system"},
            {"id": "o1", "object": "model", "created": 1715367049, "owned_by": "system"}
        ]
    }

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    target_model = "Thinking" if "o1" in request.model.lower() else "Instant"
    
    is_new_chat, delta_messages = current_state.get_delta_or_diverge(request.messages)
    
    # Склеиваем только дельту! Если is_new_chat, склеится вся история.
    prompt_text = format_delta_prompt(delta_messages, request.tools)
    
    if request.stream:
        async def event_generator():
            full_reply = ""
            try:
                # В browser_manager передаем флаг is_new_chat, чтобы он нажал кнопку если надо
                async for chunk in browser_manager.send_prompt_and_stream(prompt_text, target_model, is_new_chat):
                    full_reply += chunk
                    response_data = {
                        "id": "chatcmpl-proxy",
                        "object": "chat.completion.chunk",
                        "model": request.model,
                        "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}]
                    }
                    yield f"data: {json.dumps(response_data)}\n\n"
                    
                current_state.update_history(request.messages, is_new_chat, full_reply)
                _url = await browser_manager.get_current_url()
                if _url:
                    current_state.chat_urls[current_state.conv_hash(request.messages)] = _url

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
        async for chunk in browser_manager.send_prompt_and_stream(prompt_text, target_model, is_new_chat):
            full_text += chunk
            
        current_state.update_history(request.messages, is_new_chat, full_text)
        _url = await browser_manager.get_current_url()
        if _url:
            current_state.chat_urls[current_state.conv_hash(request.messages)] = _url

        return {
            "id": "chatcmpl-proxy",
            "object": "chat.completion",
            "model": request.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": full_text}, "finish_reason": "stop"}]
        }

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
