import json
import re
from typing import AsyncGenerator

# Delimiter tool protocol (<<<verb>>> form — proven non-markdown). The model emits raw blocks; we convert to native
# OpenAI tool_calls. No JSON escaping required from the model -> far more stable.
# Format: <<<VERB attr="value">>>...<<<END>>>
# DISABLED BG-family: BG|BGSTATUS|BGTAIL|BGSTOP — moved to native BASH (start /B). Restore here if needed.
_HEADER_RE = re.compile(r"<<<(WRITE|EDIT|READ|BASH|GLOB|GREP|FETCH|TODO|TASK|ASK|MSG)([^>]*)>>>", re.IGNORECASE)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_END = "<<<END>>>"
_THINKING_RE = re.compile(r'^(Thinking|Thought for \d+ seconds?)[\.\.\s]*', re.IGNORECASE)

# Number of chars to hold back while streaming WRITE body (prevents partial closing ``` fence leaking)
_WRITE_HOLDBACK = 16


class ResponseParser:
    def __init__(self):
        self.buf = ""
        self._n = 0
        # Per-WRITE incremental streaming state (reset after <<<END>>>)
        self._w_active = False     # True while inside a streaming WRITE block
        self._w_id = ""            # tool_call id for the open WRITE
        self._w_path = ""          # filePath attribute
        self._w_buf_start = 0      # index in self.buf where WRITE body starts (after header)
        self._w_streamed = 0       # code-point count of content already sent to client
        self._w_lead_done = False  # True once the fence opening line has been skipped
        self._w_lead_end = 0       # offset within body (from _w_buf_start) after the fence lead line

    def _next_id(self) -> str:
        self._n += 1
        return f"call_local_{self._n}"

    def _needs_body(self, verb: str, attrs: dict) -> bool:
        """True if the block needs a closing <<<END>>>"""
        return verb.upper() in ("WRITE", "EDIT", "BASH", "TODO", "TASK", "MSG")

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

    # ------------------------------------------------------------------
    # WRITE incremental streaming helpers
    # ------------------------------------------------------------------

    def _w_open_chunk(self, path: str, model: str) -> str:
        """First chunk: opens the tool_call with name and the arguments prefix."""
        call_id = self._w_id
        open_args = json.dumps(path, ensure_ascii=False)
        arguments_prefix = '{"filePath": ' + open_args + ', "content": "'
        data = {
            "id": "chatcmpl-proxy",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "write", "arguments": arguments_prefix},
                    }]
                },
                "finish_reason": None,
            }],
        }
        return f"data: {json.dumps(data)}\n\n"

    def _w_cont_chunk(self, piece: str, model: str) -> str:
        """Continuation chunk: JSON-escaped fragment of content (no surrounding quotes)."""
        frag = json.dumps(piece, ensure_ascii=False)[1:-1]  # strip outer quotes
        data = {
            "id": "chatcmpl-proxy",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"arguments": frag},
                    }]
                },
                "finish_reason": None,
            }],
        }
        return f"data: {json.dumps(data)}\n\n"

    def _w_close_chunk(self, model: str) -> str:
        """Closing chunk: closes the JSON string and object, sets finish_reason."""
        data = {
            "id": "chatcmpl-proxy",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"arguments": '"}'},
                    }]
                },
                "finish_reason": "tool_calls",
            }],
        }
        return f"data: {json.dumps(data)}\n\n"

    # ------------------------------------------------------------------
    # _drain — main parsing loop
    # ------------------------------------------------------------------

    def _drain(self, model: str, final: bool):
        out = []

        # ── Resume an in-progress WRITE stream ──────────────────────────
        if self._w_active:
            chunks = self._drain_write_stream(model, final)
            out.extend(chunks)
            if self._w_active:
                # Still waiting for <<<END>>> — nothing more to drain
                return out
            # WRITE finished; self.buf has been trimmed past <<<END>>>; fall through

        # ── Normal drain loop ────────────────────────────────────────────
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
            verb = m.group(1).upper()
            attrs = dict(_ATTR_RE.findall(m.group(2)))
            header_end = m.end()
            if not self._needs_body(verb, attrs):
                out.append(self._tool(verb, attrs, "", model))
                self.buf = self.buf[header_end:]
                continue

            # Body-bearing verbs -------------------------------------------
            end_idx = self.buf.find(_END, header_end)

            if verb == "WRITE":
                if end_idx != -1:
                    # <<<END>>> already present → atomic path (unchanged)
                    body = self.buf[header_end:end_idx]
                    out.append(self._tool(verb, attrs, body, model))
                    self.buf = self.buf[end_idx + len(_END):]
                    continue
                else:
                    # Enter incremental streaming mode
                    self._w_active = True
                    self._w_id = self._next_id()
                    self._w_path = attrs.get("path", "")
                    self._w_buf_start = header_end  # body starts here in self.buf
                    self._w_streamed = 0
                    self._w_lead_done = False
                    self._w_lead_end = 0
                    chunks = self._drain_write_stream(model, final)
                    out.extend(chunks)
                    if self._w_active:
                        return out
                    # WRITE done — buf already trimmed, continue outer loop
                    continue

            # All other body verbs — atomic (unchanged)
            if end_idx == -1:
                if final:
                    out.append(self._content(self.buf, model))
                    self.buf = ""
                return out
            body = self.buf[header_end:end_idx]
            out.append(self._tool(verb, attrs, body, model))
            self.buf = self.buf[end_idx + len(_END):]

        return out

    # ------------------------------------------------------------------
    # _drain_write_stream — called whenever new data may be available
    # ------------------------------------------------------------------

    def _drain_write_stream(self, model: str, final: bool) -> list:
        """Stream content from an open WRITE block.  Returns list of SSE strings.
        Clears self._w_active and trims self.buf when <<<END>>> is consumed."""
        out = []
        body_start = self._w_buf_start  # absolute index in self.buf where body starts

        # Check whether <<<END>>> has arrived
        end_idx = self.buf.find(_END, body_start)
        if end_idx == -1 and not final:
            # <<<END>>> not yet seen — stream what we safely can
            raw_body = self.buf[body_start:]
            out.extend(self._w_stream_partial(raw_body, model))
            return out

        # ───── <<<END>>> found (or final flush) ──────────────────────────
        if end_idx != -1:
            full_body = self.buf[body_start:end_idx]
        else:
            # final=True and no <<<END>>>: use whatever we have
            full_body = self.buf[body_start:]

        # Authoritative content via _strip_fence
        content = self._strip_fence(full_body)

        # Emit opening chunk only if not yet done (streamed==0 means open not sent yet)
        if self._w_streamed == 0:
            out.append(self._w_open_chunk(self._w_path, model))

        # Emit remainder that wasn't streamed yet
        remainder = content[self._w_streamed:]
        if remainder:
            out.append(self._w_cont_chunk(remainder, model))

        # Closing chunk
        out.append(self._w_close_chunk(model))

        # Trim buf past <<<END>>> (or to end if no END found)
        if end_idx != -1:
            self.buf = self.buf[end_idx + len(_END):]
        else:
            self.buf = ""

        # Reset state
        self._w_active = False
        self._w_streamed = len(content)
        return out

    def _w_stream_partial(self, raw_body: str, model: str) -> list:
        """Stream as much of raw_body as is safe, managing fence lead and holdback.
        Updates self._w_streamed, self._w_lead_done, self._w_lead_end in place.

        _strip_fence logic (mirrored here for partial streaming):
          1. s.strip("\\n")  — drop leading/trailing newlines
          2. If first remaining line starts with ``` → skip that line (fence open)
          3. If last line is ``` → skip it (fence close) — handled by holdback+flush

        self._w_lead_end is an index into raw_body (NOT the lstripped version)
        pointing to where content begins after the fence opening line (if any).
        """
        out = []

        # ── Phase 1: determine fence lead ────────────────────────────────
        if not self._w_lead_done:
            # Mirror _strip_fence: strip leading \n to find true first line
            stripped_start = len(raw_body) - len(raw_body.lstrip("\n"))
            after_lead_newlines = raw_body[stripped_start:]

            # Need at least one complete line after the leading newlines
            nl_idx = after_lead_newlines.find("\n")
            if nl_idx == -1:
                # Can't determine fence yet — hold everything
                return out

            first_line = after_lead_newlines[:nl_idx]
            if first_line.lstrip().startswith("```"):
                # Fence open line: content starts after this line's \n
                self._w_lead_end = stripped_start + nl_idx + 1
            else:
                # No fence — content starts right after the leading \n stripping
                # (strip leading \n the same way _strip_fence does strip("\n"))
                self._w_lead_end = stripped_start
            self._w_lead_done = True

        # Content within raw_body starts at _w_lead_end
        content_so_far = raw_body[self._w_lead_end:]

        # Apply holdback: hold last _WRITE_HOLDBACK chars to avoid partial closing fence
        safe_len = max(0, len(content_so_far) - _WRITE_HOLDBACK)
        streamable = content_so_far[:safe_len]

        # Only emit what hasn't been sent yet
        if len(streamable) <= self._w_streamed:
            return out

        new_piece = streamable[self._w_streamed:]
        if not new_piece:
            return out

        # First emission: send open chunk
        if self._w_streamed == 0:
            out.append(self._w_open_chunk(self._w_path, model))

        out.append(self._w_cont_chunk(new_piece, model))
        self._w_streamed += len(new_piece)
        return out

    # ------------------------------------------------------------------
    # Existing methods (unchanged)
    # ------------------------------------------------------------------

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
        if verb == "MSG":
            return self._content(self._strip_fence(body), model)
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
        content = content.replace("<<<END>>>", "")
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
