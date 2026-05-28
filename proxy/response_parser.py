import json
import re
from typing import AsyncGenerator

# Delimiter tool protocol (pandoc-style + Russian). The model emits raw blocks; we convert to native
# OpenAI tool_calls. No JSON escaping required from the model -> far more stable.
# Format: :::verb attr="value":::...:::конец:::
_HEADER_RE = re.compile(r":::([а-яё]+)\s*([^:]*?):::", re.IGNORECASE | re.UNICODE)
_ATTR_RE = re.compile(r'([а-яё]+)="([^"]*)"', re.IGNORECASE | re.UNICODE)
_END = ":::конец:::"
_THINKING_RE = re.compile(r'^(Thinking|Thought for \d+ seconds?)[\.\.\s]*', re.IGNORECASE)


class ResponseParser:
    def __init__(self):
        self.buf = ""
        self._n = 0

    def _next_id(self) -> str:
        self._n += 1
        return f"call_local_{self._n}"

    def _needs_body(self, verb: str, attrs: dict) -> bool:
        """True if the block needs a closing :::конец:::"""
        if verb == "файл" and ("создать" in attrs or "правка" in attrs):
            return True
        if verb == "оболочка":
            return True
        return False

    async def process_stream(self, chunk_stream: AsyncGenerator[str, None], model: str):
        async for chunk in chunk_stream:
            self.buf += chunk
            for out in self._drain(model, final=False):
                if out:  # _content() может вернуть "" после стрипа артефактов
                    yield out
        for out in self._drain(model, final=True):
            if out:
                yield out
        if self.buf:
            c = self._content(self.buf, model)
            if c:
                yield c
            self.buf = ""

    def _drain(self, model: str, final: bool):
        out = []
        while True:
            m = _HEADER_RE.search(self.buf)
            if not m:
                cut = self._safe_cut(self.buf, final)
                if cut > 0:
                    text, self.buf = self.buf[:cut], self.buf[cut:]
                    out.append(self._content(text, model))
                return out
            if m.start() > 0:
                out.append(self._content(self.buf[:m.start()], model))
                self.buf = self.buf[m.start():]
                continue
            verb = m.group(1)
            attrs = dict(_ATTR_RE.findall(m.group(2)))
            header_end = m.end()
            if not self._needs_body(verb, attrs):
                out.append(self._tool(verb, attrs, "", model))
                self.buf = self.buf[header_end:]
                continue
            end_idx = self.buf.find(_END, header_end)
            if end_idx == -1:
                if final:
                    out.append(self._content(self.buf, model))
                    self.buf = ""
                return out
            body = self.buf[header_end:end_idx]
            out.append(self._tool(verb, attrs, body, model))
            self.buf = self.buf[end_idx + len(_END):]
        return out

    def _safe_cut(self, s: str, final: bool) -> int:
        """How many chars are safe to emit as text now (hold a possible partial marker)."""
        if final:
            return len(s)
        # Hold partial ::: markers (1-3 colons)
        for tail in (":::", "::", ":"):
            if s.endswith(tail):
                return len(s) - len(tail)
        # If there's a ::: without a closing pair, hold from that point
        idx = s.rfind(":::")
        if idx != -1 and ":::" not in s[idx + 3:]:
            return idx
        return len(s)

    def _tool(self, verb: str, attrs: dict, body: str, model: str) -> str:
        if verb == "файл":
            if "создать" in attrs:
                return self._tool_chunk("write", {"filePath": attrs["создать"], "content": self._strip_fence(body)}, model)
            if "правка" in attrs:
                old, new = self._split_old_new(body)
                return self._tool_chunk("edit", {"filePath": attrs["правка"], "oldString": old, "newString": new}, model)
            if "читать" in attrs:
                return self._tool_chunk("read", {"filePath": attrs["читать"]}, model)
        if verb == "оболочка":
            return self._tool_chunk("bash", {"command": self._strip_fence(body.strip())}, model)
        if verb == "файлы":
            return self._tool_chunk("glob", {"pattern": attrs.get("шаблон", "")}, model)
        if verb == "текст":
            args = {"pattern": attrs.get("поиск", "")}
            if attrs.get("в"):
                args["path"] = attrs["в"]
            return self._tool_chunk("grep", args, model)
        return self._content(body, model)

    def _strip_fence(self, s: str) -> str:
        """Срезает обрамляющий markdown-фенс (```lang ... ```), если он просочился
        в контент. Обычно читалка уже отдаёт код из PRE сырьём — это страховка."""
        s = s.strip("\n")
        lines = s.split("\n")
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
        return "\n".join(lines)

    def _split_old_new(self, body: str):
        o = body.find(":::было:::")
        n = body.find(":::стало:::")
        if o != -1 and n != -1 and n > o:
            old = self._strip_fence(body[o + len(":::было:::"):n])
            new = self._strip_fence(body[n + len(":::стало:::"):])
            return old, new
        return self._strip_fence(body), ""

    def _content(self, content: str, model: str) -> str:
        # Стрип артефактов рендера, просочившихся через DOM
        content = _THINKING_RE.sub('', content)
        # Canvas: обрезаем от маркера до конца чанка
        ci = content.find("[Canvas]")
        if ci != -1:
            content = content[:ci]
        if not content.strip():
            return ""  # Не отправляем пустые чанки
        data = {
            "id": "chatcmpl-proxy",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        }
        return f"data: {json.dumps(data)}\n\n"

    def _tool_chunk(self, name: str, arguments: dict, model: str) -> str:
        data = {
            "id": "chatcmpl-proxy",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": self._next_id(),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
                    }]
                },
                "finish_reason": "tool_calls",
            }],
        }
        return f"data: {json.dumps(data)}\n\n"
