"""Нативные CDP-взаимодействия (мышь/клавиатура/DOM) — без исполнения JS в странице."""
import asyncio
import random
import re
import html as _html
from loguru import logger
import nodriver.cdp.input_ as cdp_input

from config import TYPING_DELAY


async def send_key(page, key_name: str, key_code: int):
    await page.send(cdp_input.dispatch_key_event(
        type_="keyDown", key=key_name,
        windows_virtual_key_code=key_code, native_virtual_key_code=key_code,
    ))
    await asyncio.sleep(0.05)
    await page.send(cdp_input.dispatch_key_event(
        type_="keyUp", key=key_name,
        windows_virtual_key_code=key_code, native_virtual_key_code=key_code,
    ))


async def human_type(element, text: str) -> None:
    """Печатает текст реальными key-событиями (CDP Input), посимвольно."""
    for char in text:
        await element.send_keys(char)
        await asyncio.sleep(random.uniform(*TYPING_DELAY))


async def insert_text_fast(page, text: str) -> None:
    """Быстрая нативная вставка длинного текста (CDP Input.insertText, не JS)."""
    await page.send(cdp_input.insert_text(text=text))


async def click_element(element) -> bool:
    """Реальный клик мышью (CDP Input), не JS .click(). True при успехе."""
    if not element:
        return False
    try:
        await element.scroll_into_view()
    except Exception:
        pass
    try:
        await element.mouse_click()
        return True
    except Exception as e:
        logger.debug("mouse_click не удался: {}", e)
        return False


async def find_element(page, selector: str, *, timeout: float = 10.0, interval: float = 0.3):
    """Поиск по CSS через CDP DOM (page.select), без JS."""
    elapsed = 0.0
    while elapsed < timeout:
        try:
            el = await page.select(selector, timeout=0.5)
            if el:
                return el
        except Exception:
            pass
        await asyncio.sleep(interval)
        elapsed += interval
    return None


async def scroll_bottom(page) -> None:
    """Нативный скролл вниз (CDP synthesize_scroll_gesture), без JS."""
    try:
        await page.scroll_down(90)
    except Exception:
        pass


async def human_scroll(page) -> None:
    """Человекоподобный скролл при ожидании ответа: переменная величина,
    изредка небольшой откат вверх. Нативный CDP-жест, без JS."""
    try:
        if random.random() < 0.18:
            await page.scroll_up(random.randint(20, 60))
        else:
            await page.scroll_down(random.randint(40, 130))
    except Exception:
        pass


def current_url(page) -> str:
    """URL вкладки из CDP target info (без JS)."""
    try:
        if page.target and page.target.url:
            return page.target.url
    except Exception:
        pass
    return ""


_BR = re.compile(r"<br\s*/?>", re.I)
_BLOCK_CLOSE = re.compile(r"</(p|div|pre|li|ul|ol|h[1-6]|tr|table|blockquote|section|article)>", re.I)
_TAG = re.compile(r"<[^>]+>")


_RECONSTRUCT_FN = """() => {
    const sel = document.querySelectorAll('div[data-message-author-role="assistant"]');
    if (!sel.length) return "";
    const last = sel[sel.length-1];
    const root = last.querySelector(".markdown") || last;

    const skip = (el) => {
        const t = el.tagName;
        if (t === 'BUTTON' || t === 'DETAILS' || t === 'SUMMARY') return true;
        const cn = (typeof el.className === 'string') ? el.className : '';
        if (cn.indexOf('thinking') !== -1 || cn.indexOf('thought') !== -1) return true;
        return false;
    };

    const inline = (node) => {
        let s = "";
        node.childNodes.forEach((ch) => {
            if (ch.nodeType === 3) { s += ch.textContent; return; }
            if (ch.nodeType !== 1) return;
            if (skip(ch)) return;
            const t = ch.tagName;
            if (t === 'BR') { s += "\\n"; return; }
            if (t === 'STRONG' || t === 'B') { s += "**" + inline(ch) + "**"; return; }
            if (t === 'EM' || t === 'I') { s += "*" + inline(ch) + "*"; return; }
            if (t === 'DEL' || t === 'S') { s += "~~" + inline(ch) + "~~"; return; }
            if (t === 'CODE') { s += "`" + (ch.textContent || "") + "`"; return; }
            if (t === 'A') {
                const href = ch.getAttribute('href') || "";
                const txt = inline(ch);
                if (/^https?:\\/\\//i.test(href)) { s += "[" + txt + "](" + href + ")"; }
                else { s += txt; }
                return;
            }
            s += inline(ch);
        });
        return s;
    };

    const listItems = (listEl, depth, ordered) => {
        let out = "", i = 1;
        listEl.childNodes.forEach((li) => {
            if (li.nodeType !== 1 || li.tagName !== 'LI') return;
            let head = "", nested = "";
            li.childNodes.forEach((c) => {
                if (c.nodeType === 1 && (c.tagName === 'UL' || c.tagName === 'OL')) {
                    nested += listItems(c, depth + 1, c.tagName === 'OL');
                } else if (c.nodeType === 3) {
                    head += c.textContent;
                } else if (c.nodeType === 1) {
                    if (!skip(c)) head += inline(c);
                }
            });
            const pad = "  ".repeat(depth);
            const marker = ordered ? (i + ". ") : "- ";
            out += pad + marker + head.trim() + "\\n" + nested;
            i++;
        });
        return out;
    };

    const block = (node, depth) => {
        let out = "";
        node.childNodes.forEach((ch) => {
            if (ch.nodeType === 3) {
                const tx = ch.textContent;
                if (tx && tx.trim()) out += tx.trim() + "\\n\\n";
                return;
            }
            if (ch.nodeType !== 1) return;
            if (skip(ch)) return;
            const t = ch.tagName;
            if (/^H[1-6]$/.test(t)) {
                const lvl = parseInt(t.charAt(1), 10);
                out += "#".repeat(lvl) + " " + inline(ch).trim() + "\\n\\n";
            } else if (t === 'P') {
                const tx = inline(ch).trim();
                if (tx) out += tx + "\\n\\n";
            } else if (t === 'PRE') {
                const codeEl = ch.querySelector('code');
                const code = codeEl ? codeEl.innerText : "";
                out += code.replace(/\\n+$/, "") + "\\n\\n";
            } else if (t === 'UL') {
                out += listItems(ch, depth, false) + "\\n";
            } else if (t === 'OL') {
                out += listItems(ch, depth, true) + "\\n";
            } else if (t === 'BLOCKQUOTE') {
                const inner = block(ch, depth).trim();
                inner.split("\\n").forEach((ln) => { out += "> " + ln + "\\n"; });
                out += "\\n";
            } else if (t === 'HR') {
                out += "---\\n\\n";
            } else if (t === 'TABLE') {
                out += (ch.innerText || "") + "\\n\\n";
            } else {
                out += block(ch, depth);
            }
        });
        return out;
    };

    let result = block(root, 0);
    result = result.replace(/\\n{3,}/g, "\\n\\n");
    return result.trim();
}"""


def _clean_text(txt: str) -> str:
    """Применяет одинаковые фильтры к текстам из read_last_assistant и read_stream_buffer.

    Фильтрация:
    - Обрезаем всё от маркера [Canvas] до конца
    - Стрипуем Thinking-префикс
    - Убираем битые цитатные якоря *]()
    - Схлопываем 3+ идущих подряд переносов
    - Финальный strip()
    """
    # Стрип Canvas — обрезаем ВСЁ от маркера [Canvas] до конца
    canvas_idx = txt.find("[Canvas]")
    if canvas_idx != -1:
        txt = txt[:canvas_idx]
    # Стрип Thinking-префикса (если просочился через DOM)
    txt = re.sub(r'^(Thinking|Thought for \d+ seconds?)[\.\s]*', '', txt, flags=re.IGNORECASE)
    txt = re.sub(r'\*\]\(\)', '', txt)          # битый цитатный якорь ChatGPT
    txt = re.sub(r'\n{3,}', '\n\n', txt)        # схлопнуть пустые строки от вырезанного
    return txt.strip()


def html_to_text(html: str) -> str:
    """Грубая конвертация HTML→текст с сохранением переносов (без JS innerText)."""
    if not html:
        return ""
    s = _BR.sub("\n", html)
    s = _BLOCK_CLOSE.sub(lambda m: m.group(0) + "\n", s)
    s = _TAG.sub("", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


async def read_last_assistant(page) -> str:
    """Текст последнего сообщения ассистента. Чтение через evaluate (быстро;
    страница не видит CDP-чтения). Весь ВВОД остаётся нативным.

    Фильтрация:
    - Пропускает DETAILS/SUMMARY элементы (Thinking-индикатор ChatGPT)
    - Пропускает BUTTON элементы (кнопки интерфейса)
    - Стрипает [Canvas] маркер и весь хвост после него
    - Стрипает битые цитатные якоря *]()
    """
    try:
        txt = await page.evaluate(f"({_RECONSTRUCT_FN})()") or ""
        return _clean_text(txt)
    except Exception:
        return ""


async def inject_stream_observer(page) -> None:
    """Инъектит пассивный MutationObserver: на каждую мутацию пересобирает чистый
    текст последнего assistant-сообщения в window.__pxFull. Идемпотентно
    (повторный вызов не плодит обсерверы). Чтение становится копеечным."""
    js = f'''
    (() => {{
        try {{
            window.__pxReconstruct = {_RECONSTRUCT_FN};
            window.__pxFull = window.__pxReconstruct();
            if (window.__pxObs) return;
            const root = document.querySelector('main') || document.body;
            window.__pxObs = new MutationObserver(() => {{
                try {{ window.__pxFull = window.__pxReconstruct(); }} catch (e) {{}}
            }});
            window.__pxObs.observe(root, {{childList: true, subtree: true, characterData: true}});
        }} catch (e) {{}}
    }})()
    '''
    try:
        await page.evaluate(js)
    except Exception as e:
        logger.debug("inject_stream_observer failed: {}", e)


async def read_stream_buffer(page) -> str:
    """Дешёвое чтение предрассчитанного обсервером текста (window.__pxFull).
    Без обхода DOM в Python. Те же фильтры, что и read_last_assistant."""
    try:
        txt = await page.evaluate("window.__pxFull || ''") or ""
        return _clean_text(txt)
    except Exception:
        return ""
