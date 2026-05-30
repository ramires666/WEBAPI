import urllib.request, json, sys, time, re

TOOLS = [
    {"type": "function", "function": {"name": "write", "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}, "content": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "edit", "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}, "oldString": {"type": "string"}, "newString": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "read", "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "glob", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "grep", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}}}},
]


def stream_request(messages, model="Instant"):
    payload = {"model": model, "stream": True, "tools": TOOLS, "messages": messages}
    req = urllib.request.Request("http://127.0.0.1:8000/v1/chat/completions",
                                 data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    content = ""
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
                content += delta["content"]
    return content


def run(tag: str):
    messages = [{"role": "system", "content": "You are Kilo. <env>cwd=W:/test</env>"}]

    t0 = time.monotonic()
    print(f"[{tag}] STEP1 (start): POSTing BG command...")
    user1 = f'запусти через BG ровно такую команду — помести её ВНУТРЬ блока (не в атрибут):\n<<<BG>>>\necho tick-{tag}-1 & echo tick-{tag}-2 & echo tick-{tag}-3 & ping -n 181 127.0.0.1 -w 1000 >nul\n<<<END>>>'
    messages.append({"role": "user", "content": user1})
    content_1 = stream_request(messages)
    elapsed = time.monotonic() - t0

    bg_id_match = re.search(r"bg_id=([0-9a-f]+)", content_1)
    if not bg_id_match:
        print(f"[{tag}] STEP1 FAIL no bg_id in: {repr(content_1[:200])}")
        sys.exit(1)

    bg_id = bg_id_match.group(1)
    print(f"[{tag}] STEP1 OK bg_id={bg_id} (took {elapsed:.1f}s)")
    messages.append({"role": "assistant", "content": content_1})

    t0 = time.monotonic()
    print(f"[{tag}] STEP2 (status): checking BGSTATUS...")
    user2 = f'проверь статус процесса {bg_id}. Эмить ТОЛЬКО блок: <<<BGSTATUS id="{bg_id}">>>'
    messages.append({"role": "user", "content": user2})
    content_2 = stream_request(messages)
    elapsed = time.monotonic() - t0

    if "alive=true" not in content_2 or "pid=" not in content_2 or bg_id not in content_2:
        print(f"[{tag}] STEP2 FAIL missing expected fields in: {repr(content_2[:200])}")
        sys.exit(1)

    print(f"[{tag}] STEP2 OK {repr(content_2[:100])} (took {elapsed:.1f}s)")
    messages.append({"role": "assistant", "content": content_2})

    t0 = time.monotonic()
    print(f"[{tag}] STEP3 (tail): reading log tail...")
    user3 = f'покажи последние строки лога процесса {bg_id}. Эмить ТОЛЬКО блок: <<<BGTAIL id="{bg_id}" lines="5">>>'
    messages.append({"role": "user", "content": user3})
    content_3 = stream_request(messages)
    elapsed = time.monotonic() - t0

    if f"tick-{tag}-" not in content_3:
        print(f"[{tag}] STEP3 FAIL no output marker in: {repr(content_3[:200])}")
        sys.exit(1)

    print(f"[{tag}] STEP3 OK found tick-{tag}- (took {elapsed:.1f}s)")
    messages.append({"role": "assistant", "content": content_3})

    t0 = time.monotonic()
    print(f"[{tag}] STEP4 (stop): stopping process...")
    user4 = f'останови процесс {bg_id}. Эмить ТОЛЬКО блок: <<<BGSTOP id="{bg_id}">>>'
    messages.append({"role": "user", "content": user4})
    content_4 = stream_request(messages)
    elapsed = time.monotonic() - t0

    if "stopped" not in content_4 or "exit_code=" not in content_4 or bg_id not in content_4:
        print(f"[{tag}] STEP4 FAIL missing expected fields in: {repr(content_4[:200])}")
        sys.exit(1)

    print(f"[{tag}] STEP4 OK {repr(content_4[:100])} (took {elapsed:.1f}s)")
    print(f"[{tag}] === ALL 4 STEPS PASSED ===")


if __name__ == "__main__":
    tag = sys.argv[1] if len(sys.argv) > 1 else "alpha"
    run(tag)
