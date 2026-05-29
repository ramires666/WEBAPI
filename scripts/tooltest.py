import urllib.request, json, sys, time

TOOLS = [
    {"type": "function", "function": {"name": "write", "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}, "content": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "edit", "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}, "oldString": {"type": "string"}, "newString": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "read", "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "glob", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "grep", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}}}},
]


def run(task: str, model: str = "Instant"):
    payload = {"model": model, "stream": True, "tools": TOOLS, "messages": [
        {"role": "system", "content": "You are Kilo. <env>cwd=W:/test</env>"},
        {"role": "user", "content": task}]}
    req = urllib.request.Request("http://127.0.0.1:8000/v1/chat/completions",
                                 data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    tc, txt = [], ""
    with urllib.request.urlopen(req, timeout=180) as r:
        for raw in r:
            s = raw.decode("utf-8", "replace").strip()
            if not s.startswith("data: ") or s == "data: [DONE]":
                continue
            try:
                delta = json.loads(s[6:])["choices"][0]["delta"]
            except Exception:
                continue
            if "content" in delta:
                txt += delta["content"]
            if "tool_calls" in delta:
                tc.append(delta["tool_calls"][0]["function"])
    print("TASK:", task[:80])
    print("TOOL CALLS:", len(tc))
    for c in tc:
        try:
            args = json.loads(c["arguments"])
        except Exception:
            args = {"_raw": c["arguments"]}
        compact = {}
        for k, v in (args.items() if isinstance(args, dict) else []):
            if isinstance(v, str) and len(v) > 100:
                compact[k] = v[:100] + "...(%d chars)" % len(v)
            else:
                compact[k] = v
        print("  -", c["name"], compact)
    print("PLAIN TEXT len:", len(txt), "| head:", repr(txt[:160]))
    print("-" * 60)


if __name__ == "__main__":
    # Usage: python scripts/tooltest.py "<task>" [Instant|Thinking]
    task = sys.argv[1] if len(sys.argv) > 1 else "say hi"
    model = sys.argv[2] if len(sys.argv) > 2 else "Instant"
    run(task, model)
