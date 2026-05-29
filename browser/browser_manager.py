"""Управление браузером и оркестрация диалога с ChatGPT — 100% нативный CDP, без JS."""
import asyncio
import random
import os
import shutil
import time
import warnings
from loguru import logger
import nodriver as uc
import nodriver.cdp.browser as cdp_browser
import nodriver.cdp.emulation as cdp_emulation
import nodriver.cdp.page as cdp_page

from config import (
    CHATGPT_URL, GENERATION_TIMEOUT, TEMP_DOWNLOADS,
)
from browser.cdp_native import (
    send_key, human_type, insert_text_fast, click_element, find_element,
    scroll_bottom, human_scroll, current_url, read_last_assistant,
)
from browser.file_extractor import click_download_buttons, collect_files

warnings.filterwarnings('ignore', category=ResourceWarning)

STOP_SELECTOR = 'button[aria-label="Stop generating"], button[data-testid="stop-button"]'
SEND_SELECTOR = '[data-testid="send-button"], button[data-testid="composer-send-button"]'
COMPOSER_SELECTOR = "#prompt-textarea"
PERSONALITY_YES = 'button[aria-label="Yes, I like this personality"]'
MODEL_KEYWORDS = ("instant", "thinking", "auto", "gpt", "chatgpt", "model", "модель")


class BrowserManager:
    def __init__(self, profile_name: str, profiles_dir: str, work_dir: str):
        self.profile_name = profile_name
        self.profiles_dir = profiles_dir
        self.work_dir = work_dir
        self.browser = None
        self.page = None
        self._lock = asyncio.Lock()

    def setup_profile(self):
        src = os.path.join(self.profiles_dir, self.profile_name)
        dst = os.path.join(self.work_dir, self.profile_name)
        if not os.path.exists(src):
            raise FileNotFoundError(f"[{self.profile_name}] Profile not found: {src}")
        if not os.path.exists(dst):
            logger.info("[{}] Копирую профиль {} → {}", self.profile_name, src, dst)
            shutil.copytree(src, dst, dirs_exist_ok=True)

    async def start_browser(self):
        self.setup_profile()
        config = uc.Config()
        config.user_data_dir = os.path.join(self.work_dir, self.profile_name)
        config.add_argument("--profile-directory=Default")
        self.browser = await uc.start(config)
        self.page = await self.browser.get(CHATGPT_URL)
        await asyncio.sleep(4)

        # Эмуляция фокуса — чтобы ChatGPT не throttle'ил стрим в фоновых вкладках
        try:
            await self.page.send(cdp_emulation.set_focus_emulation_enabled(enabled=True))
            logger.info("[{}] Focus emulation: ON", self.profile_name)
        except Exception as e:
            logger.warning("[{}] Focus emulation failed: {}", self.profile_name, e)
        try:
            await self.page.send(cdp_page.set_web_lifecycle_state(state="active"))
            logger.info("[{}] Lifecycle: active", self.profile_name)
        except Exception as e:
            logger.warning("[{}] Lifecycle set failed: {}", self.profile_name, e)

        os.makedirs(TEMP_DOWNLOADS, exist_ok=True)
        try:
            await self.page.send(cdp_browser.set_download_behavior(behavior="allow", download_path=TEMP_DOWNLOADS))
            logger.info("[{}] Загрузки → {}", self.profile_name, TEMP_DOWNLOADS)
        except Exception as e:
            logger.warning("[{}] Не удалось установить download behavior: {}", self.profile_name, e)
        logger.info("[{}] Браузер запущен и готов.", self.profile_name)

    async def stop_browser(self):
        if self.browser:
            logger.info("[{}] Останавливаю браузер...", self.profile_name)
            self.browser.stop()
            logger.info("[{}] Браузер остановлен.", self.profile_name)

    async def get_current_url(self) -> str:
        return current_url(self.page)

    async def select_model(self, target_model: str):
        """Переключение модели реальным кликом. Кнопка-переключатель ChatGPT —
        это button[aria-haspopup="menu"], чей текст = текущая модель
        (Instant/Thinking/Auto). Переключаем только если текущая != целевая —
        работает в обе стороны (в т.ч. Thinking->Instant)."""
        target = target_model.strip().lower()
        MODEL_NAMES = ("instant", "thinking", "auto")
        try:
            buttons = await self.page.select_all("button")
        except Exception:
            buttons = []
        selector_btn = None
        current = ""
        for b in buttons or []:
            try:
                if b.attrs.get("aria-haspopup") != "menu":
                    continue
                t = (b.text_all or "").strip().lower()
                if t in MODEL_NAMES:
                    selector_btn = b
                    current = t
                    break
            except Exception:
                continue
        if not selector_btn:
            logger.warning("[{}] Кнопка выбора модели не найдена", self.profile_name)
            return
        if current == target:
            logger.info("[{}] Модель уже '{}' — переключение не нужно", self.profile_name, target_model)
            return
        if not await click_element(selector_btn):
            logger.warning("[{}] Не удалось кликнуть переключатель модели", self.profile_name)
            return
        await asyncio.sleep(1.2)
        try:
            items = await self.page.select_all(
                '[role="menuitem"], [role="menuitemradio"], [role="option"]'
            )
        except Exception:
            items = []
        logger.info("[{}] МЕНЮ МОДЕЛЕЙ ({}): {}", self.profile_name, len(items or []),
                    [(it.text_all or "").strip()[:50] for it in (items or [])])
        for it in items or []:
            try:
                t = (it.text_all or "").strip().lower()
                if t == target or (target in t and "project" not in t):
                    await click_element(it)
                    await asyncio.sleep(1.0)
                    logger.info("[{}] Модель выбрана: {}", self.profile_name, target_model)
                    return
            except Exception:
                continue
        logger.warning("[{}] Пункт модели '{}' не найден в меню", self.profile_name, target_model)

    async def _handle_personality_popup(self):
        """Иногда (35%) кликает 'палец вверх' на поп-апе personality — мышью."""
        try:
            btn = await find_element(self.page, PERSONALITY_YES, timeout=0.5)
            if not btn:
                return
            if random.random() < 0.35:
                if await click_element(btn):
                    logger.info("[{}] Поп-ап personality: палец вверх", self.profile_name)
            else:
                logger.info("[{}] Поп-ап personality: пропускаем", self.profile_name)
        except Exception:
            pass

    async def send_prompt_and_stream(self, prompt_text: str, target_model: str, is_new_chat: bool, chat_url: str = None):
        async with self._lock:
            if is_new_chat:
                logger.info("[{}] === НАЧАЛО НОВОГО ЧАТА ===", self.profile_name)
                await self.page.get(CHATGPT_URL)
                await asyncio.sleep(3.5)
            elif chat_url:
                current_url = await self.get_current_url()
                if current_url.rstrip("/") != chat_url.rstrip("/"):
                    logger.info("[{}] === ПЕРЕХОД В ЧАТ {} ===", self.profile_name, chat_url)
                    await self.page.get(chat_url)
                    await asyncio.sleep(3.0)
                else:
                    logger.info("[{}] === ЧАТ {} УЖЕ ОТКРЫТ ===", self.profile_name, chat_url)
            else:
                logger.info("[{}] === ПРОДОЛЖЕНИЕ ТЕКУЩЕГО ЧАТА ===", self.profile_name)

            # Модель применяем перед КАЖДЫМ сообщением (её можно менять в любой
            # момент диалога), а не только в новом чате. Для Instant — no-op.
            await self.select_model(target_model)

            textarea = await find_element(self.page, COMPOSER_SELECTOR, timeout=10)
            if not textarea:
                yield "Error: prompt textarea not found"
                return

            logger.info("[{}] Ввод промпта (длина {} символов)...", self.profile_name, len(prompt_text))
            await click_element(textarea)
            await asyncio.sleep(0.4)
            if len(prompt_text) > 2000:
                logger.info("[{}] Длинный текст — нативная вставка insert_text...", self.profile_name)
                await insert_text_fast(self.page, prompt_text)
                await asyncio.sleep(0.8)
            else:
                await human_type(textarea, prompt_text)

            await asyncio.sleep(0.8)
            send_btn = await find_element(self.page, SEND_SELECTOR, timeout=3)
            if not await click_element(send_btn):
                await send_key(self.page, "Enter", 13)

            logger.info("[{}] Запрос отправлен. Ждём генерацию...", self.profile_name)
            interval = 0.4
            start = time.time()
            last_change = start
            last_scroll = start
            seen_activity = False
            baseline = await read_last_assistant(self.page)  # текст ДО ответа (для продолжения чата)
            last_text = baseline
            prev_text = baseline
            emitted = ""        # часть НОВОГО ответа, уже отданная клиенту
            started = False     # новый ответ начал появляться

            def _cpl(a, b):
                n = min(len(a), len(b))
                i = 0
                while i < n and a[i] == b[i]:
                    i += 1
                return i

            while time.time() - start < GENERATION_TIMEOUT:
                stop_present = False
                try:
                    stop_present = bool(await self.page.evaluate(
                        'document.querySelector(\'button[aria-label="Stop generating"], button[data-testid="stop-button"]\') ? true : false'
                    ))
                except Exception:
                    pass

                current_text = await read_last_assistant(self.page)
                if current_text != last_text:
                    last_text = current_text
                    seen_activity = True
                    last_change = time.time()

                # новый ответ начался (отличается от того, что было до отправки)
                if not started and current_text and current_text != baseline:
                    started = True
                    prev_text = current_text

                # Стабильный стриминг: отдаём только префикс, совпавший в ДВУХ
                # подряд чтениях DOM (исключает искажения от ре-рендеров), и только
                # как чистое продолжение уже отданного.
                if started:
                    sp = _cpl(prev_text, current_text)
                    prev_text = current_text
                    if sp > len(emitted) and current_text[:len(emitted)] == emitted:
                        chunk = current_text[len(emitted):sp]
                        emitted = current_text[:sp]
                        if chunk:
                            yield chunk

                if stop_present:
                    seen_activity = True
                    last_change = time.time()

                # завершено: новый непустой текст, Stop исчез, текст стабилен ~2с
                if last_text and last_text != baseline and not stop_present and (time.time() - last_change) >= 2.0:
                    logger.info("[{}] Генерация завершена.", self.profile_name)
                    with open("temp/final_generation_dump.txt", "w", encoding="utf-8") as f:
                        f.write(last_text)
                    break
                # ответ так и не появился — не висим
                if (not last_text or last_text == baseline) and (time.time() - start) > 25:
                    logger.warning("[{}] Ответ не появился за 25с — выходим.", self.profile_name)
                    break

                if time.time() - last_scroll >= random.uniform(1.5, 3.5):
                    await human_scroll(self.page)
                    last_scroll = time.time()
                await asyncio.sleep(interval)

            # Досыл хвоста: гарантируем, что отдан ПОЛНЫЙ чистый финальный текст
            # (всё, что не успели отдать стабильным стримингом).
            final_text = last_text if last_text != baseline else ""
            if final_text:
                if final_text.startswith(emitted):
                    tail = final_text[len(emitted):]
                else:
                    tail = final_text[_cpl(emitted, final_text):]
                if tail:
                    yield tail
            await scroll_bottom(self.page)
            await asyncio.sleep(0.5)
            await click_download_buttons(self.page)
            await asyncio.sleep(2.0)
            blob = await collect_files(self.page)
            if blob:
                yield blob

            await self._handle_personality_popup()
