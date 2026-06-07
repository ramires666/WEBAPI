"""РАЗОВЫЙ ПРОБНИК ЖИВОГО СТРИМА: шлёт прозовый запрос в прокси и печатает
КАЖДЫЙ content-чанк с отметкой времени (+X.Xs) — видно, идёт ли markdown
живьём или валится одним куском в конце.

ВАЖНО: пульс (heartbeat) тоже приходит в delta.content (⏳/💬/📝/ · /(Nс)).
Чтобы «разброс» не врал, чанки КЛАССИФИЦИРУЮТСЯ: пульс vs реальный контент.
Метрики (1-й контент, разброс) считаются ТОЛЬКО по реальному контенту.
Также ловит утечку сырого '<<<' в видимый текст и считает tool_calls.
Usage: python scripts/_stream_probe.py "<уникальный промпт>" [model]
"""
import json, re, sys, time, urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

URL = "http://127.0.0.1:8000/v1/chat/completions"
prompt = sys.argv[1] if len(sys.argv) > 1 else "Объясни в трёх абзацах простыми словами что такое идемпотентность в HTTP."
model = sys.argv[2] if len(sys.argv) > 2 else "instant"

# Пульс-маркеры: эмодзи-статусы из api_server + точки/секунды.
_HB_EMOJI = "⏳💬📝✏️📖🖥🔍🔎🌐📋❓💭"
_HB_DOTS_RE = re.compile(r'^\s*·\s*$')
_HB_SECS_RE = re.compile(r'^\s*\(\d+с\)\s*$')


def is_heartbeat(piece: str) -> bool:
    p = piece.lstrip("\n").lstrip()
    if not p:
        return _HB_DOTS_RE.match(piece) or _HB_SECS_RE.match(piece)
    if p[0] in _HB_EMOJI:
        return True
    if _HB_DOTS_RE.match(piece) or _HB_SECS_RE.match(piece):
        return True
    return False


payload = {"model": model, "stream": True,
           "messages": [{"role": "user", "content": prompt}]}
req = urllib.request.Request(URL, data=json.dumps(payload).encode(),
                             headers={"Content-Type": "application/json"})

t0 = time.perf_counter()
t_first_real = None
real_chunks = []      # (elapsed, piece) — только реальный контент
hb_count = 0
tool_calls = 0
leaked = False
print(f"PROMPT[{model}]: {prompt[:60]!r}")
with urllib.request.urlopen(req, timeout=300) as r:
    for raw in r:
        s = raw.decode("utf-8", "replace").strip()
        if not s.startswith("data: ") or s == "data: [DONE]":
            continue
        try:
            delta = json.loads(s[6:])["choices"][0]["delta"]
        except Exception:
            continue
        if "content" in delta and delta["content"]:
            el = time.perf_counter() - t0
            piece = delta["content"]
            if "<<<" in piece:
                leaked = True
            if is_heartbeat(piece):
                hb_count += 1
                continue
            if t_first_real is None:
                t_first_real = el
            real_chunks.append((el, piece))
            preview = piece.replace("\n", "\\n")[:50]
            print(f"  [+{el:5.1f}s] {len(piece):4d}c | {preview}")
        if "tool_calls" in delta and delta["tool_calls"][0].get("function", {}).get("name"):
            tool_calls += 1

total = time.perf_counter() - t0
full = "".join(p for _, p in real_chunks)
print("-" * 60)
print(f"📊 1-й РЕАЛ.контент={t_first_real and round(t_first_real,1)}s  total={round(total,1)}s  "
      f"реал-чанков={len(real_chunks)}  пульс-чанков={hb_count}  tool_calls={tool_calls}")
print(f"   leaked '<<<'={leaked}  total_real_len={len(full)}")
# разброс по РЕАЛЬНОМУ контенту: >0 и чанков>1 → MSG/проза реально течёт живьём
if len(real_chunks) >= 2:
    spread = real_chunks[-1][0] - real_chunks[0][0]
    verdict = "ЖИВОЙ стрим" if spread > 1.0 else "почти атомарно (think-then-burst)"
    print(f"   ⏱ разброс реал.контента={round(spread,1)}s → {verdict}")
elif len(real_chunks) == 1:
    print(f"   ⏱ один реал-чанк → атомарно (модель бёрстнула весь MSG в конце)")
