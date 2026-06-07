"""Реплей РЕАЛЬНОГО захваченного запроса Kilo (temp/request_dump_*.json) в прокси.
Синтетика врёт — этот харнесс шлёт настоящий систем-промпт + реальные тулзы + сообщения,
как их прислал живой Kilo. Печатает tool_calls, тайминг 1-го чанка, целостность кода.

Usage:
  python scripts/replay.py                 # последний request_dump_*.json
  python scripts/replay.py <path-or-glob>  # конкретный дамп
"""
import glob, json, os, sys, time, urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DUMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp")
URL = "http://127.0.0.1:8000/v1/chat/completions"


def pick_dump(arg):
    if arg:
        hits = sorted(glob.glob(arg))
        if hits:
            return hits[-1]
    cands = glob.glob(os.path.join(DUMP_DIR, "request_dump_*.json"))
    if not cands:
        sys.exit("Нет request_dump_*.json в temp/ — сначала прогони запрос из Kilo.")
    return max(cands, key=os.path.getmtime)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    path = pick_dump(arg)
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    payload["stream"] = True

    sys_len = 0
    for m in payload.get("messages", []):
        if m.get("role") == "system":
            c = m.get("content", "")
            sys_len = len(c if isinstance(c, str) else json.dumps(c))
            break
    print(f"ДАМП: {os.path.basename(path)}")
    print(f"  model={payload.get('model')} tools={len(payload.get('tools', []))} "
          f"messages={len(payload.get('messages', []))} system_len={sys_len}")

    req = urllib.request.Request(URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    t_first = None
    acc, txt, n_content = {}, "", 0
    with urllib.request.urlopen(req, timeout=300) as r:
        for raw in r:
            s = raw.decode("utf-8", "replace").strip()
            if not s.startswith("data: ") or s == "data: [DONE]":
                continue
            try:
                delta = json.loads(s[6:])["choices"][0]["delta"]
            except Exception:
                continue
            if t_first is None:
                t_first = time.perf_counter() - t0
            if "content" in delta and delta["content"]:
                txt += delta["content"]
                n_content += 1
            if "tool_calls" in delta:
                tc0 = delta["tool_calls"][0]
                idx = tc0.get("index", 0)
                fn = tc0.get("function", {})
                if fn.get("name"):
                    acc[idx] = {"name": fn["name"], "arguments": fn.get("arguments", "")}
                elif idx in acc:
                    acc[idx]["arguments"] += fn.get("arguments", "")
    total = time.perf_counter() - t0

    print(f"\n📊 ТАЙМИНГ: 1-й чанк={t_first and round(t_first, 2)}s  total={round(total, 2)}s  "
          f"content-чанков={n_content}")
    print(f"TOOL CALLS: {len(acc)}")
    for i in sorted(acc):
        c = acc[i]
        try:
            args = json.loads(c["arguments"])
        except Exception:
            args = {"_raw_len": len(c["arguments"]), "_raw_head": c["arguments"][:80]}
        info = {}
        for k, v in (args.items() if isinstance(args, dict) else []):
            if isinstance(v, str) and len(v) > 60:
                info[k] = (f"len={len(v)} dunder={'__' in v} "
                           f"indent={'    ' in v or chr(9) in v} head={v[:50]!r}")
            else:
                info[k] = v
        print(f"  [{i}] {c['name']}: {info}")
    if txt:
        print(f"\nPLAIN TEXT len={len(txt)}  head={txt[:200]!r}")
    print("-" * 70)


if __name__ == "__main__":
    main()
