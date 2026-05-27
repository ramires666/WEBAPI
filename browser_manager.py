"""Управление браузером и оркестрация диалога с ChatGPT — 100% нативный CDP, без JS."""
import asyncio
import random
import os
import shutil
import warnings
from loguru import logger
import nodriver as uc
import nodriver.cdp.browser as cdp_browser

from config import (
    ORIGINAL_CHROME_USER_DATA, WORKING_PROFILE_DIR, CHROME_PROFILE_NAME,
    CHATGPT_URL, GENERATION_TIMEOUT, TEMP_DOWNLOADS,
)
from cdp_native import (
    send_key, human_type, insert_text_fast, click_element, find_element,
    scroll_bottom, current_url, read_last_assistant,
)
from file_extractor import click_download_buttons, collect_files

warnings.filterwarnings('ignore', category=ResourceWarning)

STOP_SELECTOR = 'button[aria-label="Stop generating"], button[data-testid="stop-button"]'
SEND_SELECTOR = '[data-testid="send-button"], button[data-testid="composer-send-button"]'
COMPOSER_SELECTOR = "#prompt-textarea"
PERSONALITY_YES = 'button[aria-label="Yes, I like this personality"]'
MODEL_KEYWORDS = ("instant", "thinking", "auto", "gpt", "chatgpt", "model", "модель")


class BrowserManager:
    def __init__(self):
        self.browser = None
        self.page = None
        self._lock = asyncio.Lock()

    def setup_profile(self):
        logger.info("Подготовка рабочего профиля Chrome...")
        os.makedirs(WORKING_PROFILE_DIR, exist_ok=True)
        src_local_state = os.path.join(ORIGINAL_CHROME_USER_DATA, "Local State")
        dst_local_state = os.path.join(WORKING_PROFILE_DIR, "Local State")
        if os.path.exists(src_local_state):
            shutil.copy2(src_local_state, dst_local_state)
        src_profile = os.path.join(ORIGINAL_CHROME_USER_DATA, CHROME_PROFILE_NAME)
        dst_profile = os.path.join(WORKING_PROFILE_DIR, CHROME_PROFILE_NAME)
        if os.path.exists(src_profile) and not os.path.exists(dst_profile):
            shutil.copytree(src_profile, dst_profile, dirs_exist_ok=True)

    async def start_browser(self):
        self.setup_profile()
        config = uc.Config()
        config.user_data_dir = WORKING_PROFILE_DIR
        config.add_argument(f"--profile-directory={CHROME_PROFILE_NAME}")
        self.browser = await uc.start(config)
        self.page = await self.browser.get(CHATGPT_URL)
        await asyncio.sleep(4)
        os.makedirs(TEMP_DOWNLOADS, exist_ok=True)
        try:
            await self.page.send(cdp_browser.set_download_behavior(behavior="allow", download_path=TEMP_DOWNLOADS))
            logger.info("Загрузки перенаправлены в {}", TEMP_DOWNLOADS)
        except Exception as e:
            logger.warning("Не удалось установить download behavior: {}", e)
        logger.info("Браузер успешно запущен и готов к API-запросам.")

    async def stop_browser(self):
        if self.browser:
            self.browser.stop()

    async def get_current_url(self) -> str:
        return current_url(self.page)

    async def select_model(self, target_model: str):
        """Открывает селектор модели и кликает нужный пункт — реальной мышью, без JS."""
        try:
            buttons = await self.page.select_all("button")
        except Exception:
            buttons = []
        selector_btn = None
        for b in buttons or []:
            try:
                t = (b.text_all or "").lower()
                if b.attrs.get("data-testid") == "send-button":
                    continue
                if "project" in t:
                    continue
                if any(k in t for k in MODEL_KEYWORDS):
                    selector_btn = b
                    break
            except Exception:
                continue
        if not selector_btn or not await click_element(selector_btn):
            logger.warning("Кнопка выбора модели не найдена")
            return
        await asyncio.sleep(1.2)
        try:
            items = await self.page.select_all('[role="menuitem"], [role="option"]')
        except Exception:
            items = []
        for it in items or []:
            try:
                t = (it.text_all or "").lower()
                if target_model.lower() in t and "project" not in t:
                    await click_element(it)
                    await asyncio.sleep(1.0)
                    logger.info("Модель выбрана: {}", target_model)
                    return
            except Exception:
                continue
        logger.warning("Пункт модели '{}' не найден в меню", target_model)

    async def _handle_personality_popup(self):
        """Иногда (35%) кликает 'палец вверх' на поп-апе personality — мышью."""
        try:
            btn = await find_element(self.page, PERSONALITY_YES, timeout=0.5)
            if not btn:
                return
            if random.random() < 0.35:
                if await click_element(btn):
                    logger.info("Поп-ап personality: палец вверх")
            else:
                logger.info("Поп-ап personality: пропускаем")
        except Exception:
            pass

    async def send_prompt_and_stream(self, prompt_text: str, target_model: str, is_new_chat: bool, chat_url: str = None):
        async with self._lock:
            if is_new_chat:
                logger.info("=== НАЧАЛО НОВОГО ЧАТА ===")
                await self.page.get(CHATGPT_URL)
                await asyncio.sleep(3.5)
                await self.select_model(target_model)
            elif chat_url:
                logger.info("=== ПЕРЕХОД В ЧАТ {} ===", chat_url)
                await self.page.get(chat_url)
                await asyncio.sleep(3.0)
            else:
                logger.info("=== ПРОДОЛЖЕНИЕ ТЕКУЩЕГО ЧАТА ===")

            textarea = await find_element(self.page, COMPOSER_SELECTOR, timeout=10)
            if not textarea:
                yield "Error: prompt textarea not found"
                return

            logger.info("Ввод промпта (длина {} символов)...", len(prompt_text))
            await click_element(textarea)
            await asyncio.sleep(0.4)
            if len(prompt_text) > 2000:
                logger.info("Длинный текст — нативная вставка insert_text...")
                await insert_text_fast(self.page, prompt_text)
                await asyncio.sleep(0.8)
            else:
                await human_type(textarea, prompt_text)

            await asyncio.sleep(0.8)
            send_btn = await find_element(self.page, SEND_SELECTOR, timeout=3)
            if not await click_element(send_btn):
                await send_key(self.page, "Enter", 13)

            logger.info("Запрос отправлен. Ждём генерацию...")
            elapsed = 0.0
            while elapsed < 20:
                if await find_element(self.page, STOP_SELECTOR, timeout=0.3):
                    break
                await asyncio.sleep(0.5)
                elapsed += 0.5

            elapsed = 0.0
            last_text = ""
            while elapsed < GENERATION_TIMEOUT:
                if int(elapsed * 10) % 30 == 0:
                    await scroll_bottom(self.page)
                current_text = await read_last_assistant(self.page)
                if current_text and current_text != last_text:
                    i = 0
                    n = min(len(last_text), len(current_text))
                    while i < n and last_text[i] == current_text[i]:
                        i += 1
                    chunk = current_text[i:]
                    last_text = current_text
                    if chunk:
                        yield chunk

                stop_present = bool(await find_element(self.page, STOP_SELECTOR, timeout=0.2))
                if not stop_present:
                    send_el = await find_element(self.page, SEND_SELECTOR, timeout=0.2)
                    if send_el or elapsed > 5:
                        logger.info("Генерация завершена.")
                        break
                await asyncio.sleep(0.3)
                elapsed += 0.3

            await scroll_bottom(self.page)
            await asyncio.sleep(0.5)
            await click_download_buttons(self.page)
            await asyncio.sleep(2.0)
            blob = await collect_files(self.page)
            if blob:
                yield blob

            await self._handle_personality_popup()
