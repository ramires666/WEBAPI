import json
import re
from typing import AsyncGenerator

# Delimiter tool protocol (<<<verb>>> form — proven non-markdown). The model emits raw blocks; we convert to native
# OpenAI tool_calls. No JSON escaping required from the model -> far more stable.
# Format: <<<VERB attr="value">>>...<<<END>>>
_HEADER_RE = re.compile(r"<<<(WRITE|EDIT|READ|BASH|GLOB|GREP|FETCH|TODO|TASK|ASK)([^>]*)>>>", re.IGNORECASE)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_END = "<<<END>>>"
_THINKING_RE = re.compile(r'^(Thinking|Thought for \d+ seconds?)[\.\.\s]*', re.IGNORECASE)


class ResponseParser:
    def __init__(self):
        self.buf = ""
        self._n = 0

    def _next_id(self) -> str:
        self._n += 1
        return f"call_local_{self._n}"

    def _needs_body(self, verb: str, attrs: dict) -> bool:
        """True if the block needs a closing <<<END>>>"""
        return verb.upper() in ("WRITE", "EDIT", "BASH", "TODO", "TASK")

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
        # Hold partial <<< markers
        for tail in ("<<<", "<<", "<"):
            if s.endswith(tail):
                return len(s) - len(tail)
        # If there's a <<< without a closing pair, hold from that point
        idx = s.rfind("<<<")
        if idx != -1 and ">>>" not in s[idx:]:
            return idx
        return len(s)

    def _tool(self, verb: str, attrs: dict, body: str, model: str) -> str:
        if verb == "WRITE":
            return self._tool_chunk("write", {"filePath": attrs.get("path", ""), "content": self._strip_fence(body)}, model)
        if verb == "EDIT":
            old, new = self._split_old_new(body)
            return self._tool_chunk("edit", {"filePath": attrs.get("path", ""), "oldString": old, "newString": new}, model)
        if verb == "READ":
            return self._tool_chunk("read", {"filePath": attrs.get("path", "")}, model)
        if verb == "BASH":
            return self._tool_chunk("bash", {"command": self._strip_fence(body.strip())}, model)
        if verb == "GLOB":
            return self._tool_chunk("glob", {"pattern": attrs.get("pattern", "")}, model)
        if verb == "GREP":
            args = {"pattern": attrs.get("pattern", "")}
            if attrs.get("path"):
                args["path"] = attrs["path"]
            return self._tool_chunk("grep", args, model)
        if verb == "FETCH":
            args = {"url": attrs.get("url", "")}
            if attrs.get("format"):
                args["format"] = attrs["format"]
            return self._tool_chunk("webfetch", args, model)
        if verb == "TODO":
            todos = self._parse_todo_body(body)
            return self._tool_chunk("todowrite", {"todos": todos}, model)
        if verb == "TASK":
            args = {
                "subagent_type": attrs.get("type", "general-purpose"),
                "description": attrs.get("desc", ""),
                "prompt": body.strip(),
            }
            return self._tool_chunk("task", args, model)
        if verb == "ASK":
            opts_raw = attrs.get("options", "")
            options = [{"label": o.strip()} for o in opts_raw.split("|") if o.strip()]
            question_obj = {"question": attrs.get("q", ""), "header": (attrs.get("q", "")[:12] or "Q")}
            if options:
                question_obj["options"] = options
            return self._tool_chunk("question", {"questions": [question_obj]}, model)
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
        o = body.find("<<<OLD>>>")
        n = body.find("<<<NEW>>>")
        if o != -1 and n != -1 and n > o:
            old = self._strip_fence(body[o + len("<<<OLD>>>"):n])
            new = self._strip_fence(body[n + len("<<<NEW>>>"):])
            return old, new
        return self._strip_fence(body), ""

    def _parse_todo_body(self, body: str) -> list:
        """Parse bullet-list of todos into [{content, status, priority}].
        Lines starting with '-', '*', or '1.' (numbered) become items."""
        items = []
        for line in body.strip().splitlines():
            s = line.strip()
            if not s:
                continue
            # Strip leading bullet/number markers
            for prefix in ("- ", "* ", "+ "):
                if s.startswith(prefix):
                    s = s[len(prefix):]
                    break
            else:
                # Numbered list (1. 2. ...)
                m = re.match(r"^\d+[.)]\s+(.+)$", s)
                if m:
                    s = m.group(1)
            if s:
                items.append({"content": s, "status": "pending", "priority": "medium"})
        return items

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
