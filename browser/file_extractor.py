"""Скачивание вложений и сбор файлов (temp_downloads / Canvas) — нативно, без JS."""
import os
from loguru import logger

from config import TEMP_DOWNLOADS
from browser.cdp_native import click_element, html_to_text


async def click_download_buttons(page) -> int:
    """В последнем ответе находит кнопки скачивания (behavior-btn / 'скачать') и кликает мышью."""
    clicked = 0
    try:
        els = await page.select_all('div[data-message-author-role="assistant"]')
        if not els:
            return 0
        candidates = await els[-1].query_selector_all("button, a")
    except Exception:
        return 0
    for el in candidates or []:
        try:
            label = (el.text_all or "").lower()
            aria = (el.attrs.get("aria-label", "") or "").lower()
            cls = el.attrs.get("class", "") or ""
            if ("behavior-btn" in cls or "скачать" in label or "download" in label
                    or "download" in aria or "download" in el.attrs):
                if await click_element(el):
                    clicked += 1
        except Exception:
            continue
    return clicked


async def collect_files(page) -> str:
    """Читает скачанные файлы, оформляет в блоки кода, удаляет. Fallback — Canvas (.cm-content)."""
    out = ""
    try:
        files = [f for f in os.listdir(TEMP_DOWNLOADS) if os.path.isfile(os.path.join(TEMP_DOWNLOADS, f))]
    except Exception:
        files = []
    for name in files:
        if name.endswith(".crdownload") or name.endswith(".tmp"):
            continue
        path = os.path.join(TEMP_DOWNLOADS, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                code = f.read()
            ext = os.path.splitext(name)[1].lstrip(".") or "text"
            lang = "python" if ext in ("py", "pyw") else ext
            out += f"\n\n[Скачанный файл: {name}]\n```{lang}\n{code}\n```"
        except Exception as e:
            logger.warning("Не прочитать файл {}: {}", name, e)
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
    if out:
        return out
    try:
        cm = await page.select(".cm-content", timeout=1)
        if cm:
            text = html_to_text(await cm.get_html())
            if text.strip():
                return f"\n\n[Canvas]\n```python\n{text.strip()}\n```"
    except Exception:
        pass
    return ""
