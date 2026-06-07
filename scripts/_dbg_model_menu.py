"""РАЗОВЫЙ ДИАГНОСТИК: дамп DOM меню выбора модели на живом ChatGPT.
Цепляется к Chrome по CDP debug-порту, находит триггер, открывает меню
НАСТОЯЩИМ мышиным кликом (Input.dispatchMouseEvent), снимает пункты.
Usage: python scripts/_dbg_model_menu.py <port>
"""
import json
import sys
import time
import urllib.request
import websocket  # websocket-client

PORT = sys.argv[1] if len(sys.argv) > 1 else "13550"

targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
page = next((t for t in targets if t.get("type") == "page" and "chatgpt.com" in t.get("url", "")), None)
if not page:
    page = next((t for t in targets if t.get("type") == "page"), None)
if not page:
    print(f"[{PORT}] НЕТ page-таргета"); sys.exit(0)
print(f"[{PORT}] URL: {page['url']}")

ws = websocket.create_connection(page["webSocketDebuggerUrl"], max_size=None)
_id = 0
def cmd(method, params=None):
    global _id
    _id += 1
    ws.send(json.dumps({"id": _id, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == _id:
            return msg

cmd("Runtime.enable")

def ev(js, awaitp=False):
    r = cmd("Runtime.evaluate", {"expression": js, "awaitPromise": awaitp, "returnByValue": True})
    return r.get("result", {}).get("result", {}).get("value")

# 1) список всех триггеров + выбранный модельный триггер + его rect
js1 = r"""
(() => {
  const out = {triggers: [], picked: null};
  const btns = [...document.querySelectorAll('button[aria-haspopup="menu"]')];
  for (const b of btns) out.triggers.push({
    testid: b.getAttribute('data-testid'),
    al: b.getAttribute('aria-label'),
    text: (b.innerText||'').trim().slice(0,40),
  });
  const KW=['instant','thinking','auto','extended','standard','gpt','legacy','pro'];
  const trig = btns.find(b => {
    const t=(b.innerText||'').trim().toLowerCase();
    const al=(b.getAttribute('aria-label')||'').toLowerCase();
    return KW.some(k=>t.startsWith(k)) || al.includes('model');
  });
  if (trig) {
    const r = trig.getBoundingClientRect();
    out.picked = {testid:trig.getAttribute('data-testid'), al:trig.getAttribute('aria-label'),
                  text:(trig.innerText||'').trim().slice(0,40),
                  x:r.x+r.width/2, y:r.y+r.height/2};
  }
  return JSON.stringify(out);
})()
"""
info = json.loads(ev(js1))
print("ТРИГГЕРЫ (aria-haspopup=menu):")
for t in info["triggers"]:
    print("   ", t)
print("ВЫБРАН ТРИГГЕР:", info["picked"])

if not info["picked"]:
    print("нет модельного триггера — стоп"); ws.close(); sys.exit(0)

# 2) настоящий мышиный клик по центру триггера
x, y = info["picked"]["x"], info["picked"]["y"]
for typ in ("mousePressed", "mouseReleased"):
    cmd("Input.dispatchMouseEvent", {"type": typ, "x": x, "y": y, "button": "left", "clickCount": 1})
time.sleep(1.3)

# 3) дамп открытого меню
js2 = r"""
(() => {
  const out = {menus: [], items: []};
  for (const m of document.querySelectorAll('[role="menu"]')) out.menus.push(m.outerHTML.slice(0,3500));
  const sel = '[role="menuitem"],[role="menuitemradio"],[role="option"],[data-testid*="model"],li,button';
  const seen = new Set();
  for (const it of document.querySelectorAll(sel)) {
    const role = it.getAttribute('role');
    const tid  = it.getAttribute('data-testid');
    const txt  = (it.innerText||'').trim().slice(0,60);
    // только то, что похоже на пункт модели
    const low = (txt||'').toLowerCase();
    const isModel = ['instant','thinking','auto','extended','standard','legacy','pro','gpt'].some(k=>low.includes(k));
    if (!isModel && !(tid||'').includes('model') && role!=='menuitemradio') continue;
    const key = role+'|'+tid+'|'+txt;
    if (seen.has(key)) continue; seen.add(key);
    out.items.push({role, testid:tid, checked:it.getAttribute('aria-checked'), text:txt});
  }
  return JSON.stringify(out);
})()
"""
dump = json.loads(ev(js2))
print("\nМЕНЮ [role=menu] найдено:", len(dump["menus"]))
for i, h in enumerate(dump["menus"]):
    print(f"--- menu[{i}] outerHTML (3500) ---")
    print(h)
print("\nКАНДИДАТЫ-ПУНКТЫ:")
for it in dump["items"]:
    print("   ", it)

# 4) закрыть меню
cmd("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
cmd("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
ws.close()
print(f"\n[{PORT}] готово")
