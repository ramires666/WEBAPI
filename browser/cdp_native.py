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
    js_code = '''
    (() => {
        const m = document.querySelectorAll('div[data-message-author-role="assistant"]');
        if (!m.length) return "";
        const last = m[m.length-1];
        const md = last.querySelector(".markdown");
        if (!md) return last.innerText || "";
        
        let result = "";
        for (let node of md.childNodes) {
            // Пропускаем Thinking-блоки (DETAILS/SUMMARY) и кнопки
            if (node.nodeType === 1) {
                const tag = node.tagName;
                if (tag === 'DETAILS' || tag === 'SUMMARY' || tag === 'BUTTON') continue;
                // Пропускаем div-ы с классами связанными с thinking
                if (tag === 'DIV' && node.className && 
                    (node.className.includes('thinking') || node.className.includes('thought'))) continue;
            }
            if (node.nodeType === 1 && node.tagName === 'PRE') {
                const codeEl = node.querySelector('code');
                const code = (codeEl ? codeEl.innerText : node.innerText) || node.textContent || "";
                result += code + "\\n";
            } else {
                const t = (node.nodeType === 3 ? node.textContent : (node.innerText || node.textContent)) || "";
                if (t) result += t + "\\n";
            }
        }
        return result.trim();
    })()
    '''
    try:
        txt = await page.evaluate(js_code) or ""
        # Стрип Canvas — обрезаем ВСЁ от маркера [Canvas] до конца
        # (после маркера обычно идёт дубль кода в ```-фенсе)
        canvas_idx = txt.find("[Canvas]")
        if canvas_idx != -1:
            txt = txt[:canvas_idx]
        # Стрип Thinking-префикса (если просочился через DOM)
        txt = re.sub(r'^(Thinking|Thought for \d+ seconds?)[\.\s]*', '', txt, flags=re.IGNORECASE)
        txt = re.sub(r'\*\]\(\)', '', txt)          # битый цитатный якорь ChatGPT
        txt = re.sub(r'\n{3,}', '\n\n', txt)        # схлопнуть пустые строки от вырезанного
        return txt.strip()
    except Exception:
        return ""
