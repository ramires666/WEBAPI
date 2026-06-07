"""DIAG v4: open ChatGPT model menu (real mouse click + focus shim) and dump the PORTAL html."""
import asyncio
import json
import os
import nodriver as uc
import nodriver.cdp.emulation as cdp_emulation
import nodriver.cdp.page as cdp_page

PROFILE_DIR = r"W:\_python\APIPROXY\work\Profile 4"
OUT = r"W:\_python\APIPROXY\temp\model_menu_dump_p4.txt"

FOCUS_SHIM_JS = """
(() => {
  try {
    Object.defineProperty(document, 'visibilityState', {get: () => 'visible', configurable: true});
    Object.defineProperty(document, 'hidden', {get: () => false, configurable: true});
    document.hasFocus = () => true;
    window.addEventListener('visibilitychange', e => { e.stopImmediatePropagation(); }, true);
    window.addEventListener('blur', e => { e.stopImmediatePropagation(); }, true);
  } catch (e) {}
})();
"""

OPEN_DUMP_JS = r'''JSON.stringify((() => {
  const out = {};
  const kids = [...document.body.children];
  let portal = null;
  for (let i = kids.length - 1; i >= 0; i--) {
    const c = kids[i];
    if (c.tagName === 'DIV' && (c.outerHTML || '').length > 800) { portal = c; break; }
  }
  if (!portal) { out.portal = 'none'; return out; }
  out.portalCls = (portal.className || '').toString().slice(0, 100);
  out.portalHTML = (portal.outerHTML || '').slice(0, 9000);
  const els = [];
  portal.querySelectorAll('*').forEach(e => {
    const role = e.getAttribute('role') || '';
    const tid = e.getAttribute('data-testid') || '';
    const t = (e.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 45);
    if (role || tid || e.tagName === 'BUTTON' || e.tagName === 'A' || e.tagName === 'LI') {
      els.push({tag: e.tagName, role: role, tid: tid, cls: (e.className || '').toString().slice(0, 55), text: t});
    }
  });
  out.els = els.slice(0, 70);
  return out;
})())'''


async def ev(page, js):
    res = await page.evaluate(js)
    return json.loads(res) if isinstance(res, str) else res


async def find_model_btn(page):
    NAMES = ("instant", "thinking", "auto", "extended", "standard", "legacy", "pro")
    buttons = await page.select_all("button")
    for b in buttons or []:
        try:
            hp = b.attrs.get("aria-haspopup") or ""
            tid = b.attrs.get("data-testid") or ""
            txt = (b.text_all or "").strip().lower()
        except Exception:
            continue
        if hp == "menu" and tid == "" and any(txt == n or txt.startswith(n) for n in NAMES):
            return b, txt
    return None, ""


async def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    report = {}
    config = uc.Config()
    config.user_data_dir = PROFILE_DIR
    config.add_argument("--profile-directory=Default")
    config.add_argument("--disable-background-timer-throttling")
    config.add_argument("--disable-backgrounding-occluded-windows")
    config.add_argument("--disable-renderer-backgrounding")
    browser = await uc.start(config)
    page = await browser.get("https://chatgpt.com")
    try:
        await page.send(cdp_emulation.set_focus_emulation_enabled(enabled=True))
    except Exception as e:
        report["focus_err1"] = str(e)
    try:
        await page.send(cdp_page.set_web_lifecycle_state(state="active"))
    except Exception as e:
        report["focus_err2"] = str(e)
    try:
        await page.evaluate(FOCUS_SHIM_JS)
    except Exception as e:
        report["focus_err3"] = str(e)
    try:
        await page.bring_to_front()
    except Exception as e:
        report["btf_err"] = str(e)
    await asyncio.sleep(6)

    btn, txt = await find_model_btn(page)
    report["model_btn"] = {"text": txt, "found": btn is not None}
    if btn is None:
        report["error"] = "model button not found"
    else:
        try:
            await btn.scroll_into_view()
        except Exception:
            pass
        try:
            await btn.mouse_click()
        except Exception as e:
            report["click_err"] = str(e)
        await asyncio.sleep(2.5)
        try:
            report["dump"] = await ev(page, OPEN_DUMP_JS)
        except Exception as e:
            report["dump"] = f"err: {e}"

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("model_btn:", report.get("model_btn"))
    d = report.get("dump")
    if isinstance(d, dict):
        print("portalCls:", d.get("portalCls"))
        print("els:", json.dumps(d.get("els"), ensure_ascii=False)[:2500])
        print("portalHTML:", (d.get("portalHTML") or "")[:5000])
    else:
        print("dump:", d)
    print("written:", OUT)
    try:
        browser.stop()
    except Exception:
        pass


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
