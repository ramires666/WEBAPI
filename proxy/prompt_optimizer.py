import json
from typing import List, Dict, Any

ALLOWED_TOOLS = {
    "read", "glob", "grep", "edit", "write", "bash", "execute_command",
    "task", "question", "background_process",
    "webfetch", "todowrite",
}

OPTIMIZED_SYSTEM_PROMPT = """You are Kilo, a non-interactive coding backend wired to the user's IDE through a local proxy. You do NOT chat. You act ONLY by emitting tool blocks; the proxy executes them on the user's machine and returns results. Prose you write is shown to the user but NEVER touches disk.

# How to call a tool
Emit the block EXACTLY as shown. Content is RAW — do NOT escape it, do NOT wrap it in markdown or ``` fences.

Create or overwrite a file:
<<<WRITE path="relative/path.ext">>>
<entire file content, verbatim>
<<<END>>>

Edit an existing file (replace exact text once):
<<<EDIT path="relative/path.ext">>>
<<<OLD>>>
<exact text to find>
<<<NEW>>>
<replacement text>
<<<END>>>

Read a file:
<<<READ path="relative/path.ext">>>

Run a shell command:
<<<BASH>>>
<command>
<<<END>>>

Find files by name:
<<<GLOB pattern="**/*.py">>>

Search file contents:
<<<GREP pattern="regex" path="src">>>

# Rules
- To save ANY code or file you MUST use WRITE. Never paste code as a plain or ```-fenced block instead of saving it — pasted code is discarded.
- One tool block per reply. After emitting it, stop and wait for the result.
- No Canvas, no artifacts, no download links.
- Minimal prose; prefer emitting the tool block immediately.

# Example
User: создай пятнашки в game.html
Assistant:
<<<WRITE path="game.html">>>
<!DOCTYPE html>
<html><body>...</body></html>
<<<END>>>
"""

PROXY_REMINDER = "\n\n[PROXY PROTOCOL] Respond ONLY with PROXY MARKUP blocks (<<<VERB ...>>>...<<<END>>>). Creating several files? Emit several WRITE blocks in THIS reply, one per file — NEVER bundle files into a BASH/Set-Content/echo script. No Canvas, no downloads, no attachments — any output outside markers is DISCARDED."

def optimize_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Отфильтровывает ненужные инструменты и сильно урезает описания."""
    if not tools:
        return []
    optimized = []
    for t in tools:
        if t.get("type") == "function":
            name = t.get("function", {}).get("name")
            if name in ALLOWED_TOOLS:
                # Копируем структуру, чтобы не менять оригинал
                import copy
                t_copy = copy.deepcopy(t)
                
                # Обрезаем описание самой функции
                desc = t_copy["function"].get("description", "")
                if len(desc) > 200:
                    t_copy["function"]["description"] = desc[:200] + "..."
                    
                # Обрезаем описания параметров
                params = t_copy["function"].get("parameters", {}).get("properties", {})
                for p_name, p_data in params.items():
                    p_desc = p_data.get("description", "")
                    if len(p_desc) > 100:
                        p_data["description"] = p_desc[:100] + "..."
                        
                optimized.append(t_copy)
    return optimized

def optimize_system_message(original_content: str) -> str:
    """Протокол tool-use вынесен в Custom Instructions аккаунта ChatGPT, поэтому
    из системного сообщения Kilo оставляем только <env>-блок (пути/ОС), если есть."""
    env_start = original_content.find("<env>")
    env_end = original_content.find("</env>")
    if env_start != -1 and env_end != -1:
        return original_content[env_start:env_end + 6]
    return ""
