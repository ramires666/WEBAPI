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

    async def _apply_focus_emulation(self):
        """Применить CDP-эмуляцию фокуса. Вызывать ПОСЛЕ каждой навигации
        (page.get), иначе настройка теряется и ChatGPT начинает throttle'ить
        фоновые вкладки."""
        try:
            await self.page.send(cdp_emulation.set_focus_emulation_enabled(enabled=True))
        except Exception as e:
            logger.warning("[{}] Focus emulation failed: {}", self.profile_name, e)
        try:
            await self.page.send(cdp_page.set_web_lifecycle_state(state="active"))
        except Exception as e:
            logger.warning("[{}] Lifecycle set failed: {}", self.profile_name, e)

    async def start_browser(self):
        self.setup_profile()
        config = uc.Config()
        config.user_data_dir = os.path.join(self.work_dir, self.profile_name)
        config.add_argument("--profile-directory=Default")
        self.browser = await uc.start(config)
        self.page = await self.browser.get(CHATGPT_URL)
        await asyncio.sleep(4)

        # Эмуляция фокуса — чтобы ChatGPT не throttle'ил стрим в фоновых вкладках
        await self._apply_focus_emulation()
        logger.info("[{}] Focus emulation and lifecycle applied", self.profile_name)

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
        target_low = target_model.strip().lower()
        try:
            buttons = await self.page.select_all("button")
        except Exception:
            buttons = []

        # Сбор кандидатов с aria-haspopup="menu" — диагностика
        candidates = []
        for b in buttons or []:
            try:
                if b.attrs.get("aria-haspopup") == "menu":
                    candidates.append((b, (b.text_all or "").strip()))
            except Exception:
                continue
        logger.info("[{}] КНОПКИ-МЕНЮ aria-haspopup (всего {}): {}",
                    self.profile_name, len(candidates),
                    [t[:60] for _, t in candidates])

        # Поиск кнопки-селектора модели: substring-матчинг
        selector_btn = None
        current_raw = ""
        for b, raw in candidates:
            tl = raw.lower()
            if any(k in tl for k in ("instant", "thinking", "auto")) and "project" not in tl and "share" not in tl:
                selector_btn = b
                current_raw = raw
                break
        if not selector_btn:
            logger.warning("[{}] Кнопка выбора модели не найдена (target='{}'). Кандидаты: {}",
                           self.profile_name, target_model, [t[:60] for _, t in candidates])
            return
        logger.info("[{}] ТЕКУЩАЯ МОДЕЛЬ (raw): '{}' | target: '{}'",
                    self.profile_name, current_raw, target_model)

        # Определить, нужен ли клик: текущая модель содержит target (как substring, lowercase)
        current_low = current_raw.lower()
        if target_low in current_low:
            logger.info("[{}] Модель уже соответствует target '{}' — переключение не нужно",
                        self.profile_name, target_model)
            return

        # Клик по кнопке
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

        # Выбор пункта с верификацией
        for it in items or []:
            try:
                t = (it.text_all or "").strip().lower()
                if target_low in t and "project" not in t:
                    await click_element(it)
                    await asyncio.sleep(1.2)
                    # ВЕРИФИКАЦИЯ: перечитываем кнопку
                    try:
                        buttons2 = await self.page.select_all("button")
                        verified_raw = ""
                        for b2 in buttons2 or []:
                            try:
                                if b2.attrs.get("aria-haspopup") == "menu":
                                    txt2 = (b2.text_all or "").strip()
                                    if any(k in txt2.lower() for k in ("instant", "thinking", "auto")):
                                        verified_raw = txt2
                                        break
                            except Exception:
                                continue
                        if target_low in verified_raw.lower():
                            logger.info("[{}] ✅ ВЕРИФИКАЦИЯ: модель переключена на '{}' (raw='{}')",
                                        self.profile_name, target_model, verified_raw)
                        else:
                            logger.warning("[{}] ⚠️ ВЕРИФИКАЦИЯ ПРОВАЛЕНА: target='{}', получено raw='{}'",
                                           self.profile_name, target_model, verified_raw)
                    except Exception as ve:
                        logger.warning("[{}] Верификация модели не удалась: {}", self.profile_name, ve)
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
                await self._apply_focus_emulation()
            elif chat_url:
                current_url = await self.get_current_url()
                if current_url.rstrip("/") != chat_url.rstrip("/"):
                    logger.info("[{}] === ПЕРЕХОД В ЧАТ {} ===", self.profile_name, chat_url)
                    await self.page.get(chat_url)
                    await asyncio.sleep(3.0)
                    await self._apply_focus_emulation()
                else:
                    logger.info("[{}] === ЧАТ {} УЖЕ ОТКРЫТ ===", self.profile_name, chat_url)
            else:
                logger.info("[{}] === ПРОДОЛЖЕНИЕ ТЕКУЩЕГО ЧАТА ===", self.profile_name)

            # Модель применяем перед КАЖДЫМ сообщением (её можно менять в любой
            # момент диалога), а не только в новом чате. Для Instant — no-op.
            await self.select_model(target_model)

            # Финальная верификация режима перед отправкой
            try:
                btns_check = await self.page.select_all("button")
                mode_btn_text = ""
                for bc in btns_check or []:
                    try:
                        if bc.attrs.get("aria-haspopup") == "menu":
                            tc = (bc.text_all or "").strip()
                            if any(k in tc.lower() for k in ("instant", "thinking", "auto")):
                                mode_btn_text = tc
                                break
                    except Exception:
                        continue
                logger.info("[{}] 🎯 ОТПРАВКА в режиме: '{}' (target='{}')",
                            self.profile_name, mode_btn_text or "?UNKNOWN?", target_model)
            except Exception:
                pass

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
            last_focus_refresh = start
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

            completed_normally = False
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

                # Периодический refresh CDP-эмуляции фокуса (раз в 5 секунд)
                if time.time() - last_focus_refresh >= 5.0:
                    await self._apply_focus_emulation()
                    last_focus_refresh = time.time()

                # завершено: новый непустой текст, Stop исчез, текст стабилен ~2с
                if last_text and last_text != baseline and not stop_present and (time.time() - last_change) >= 2.0:
                    # Defensive: модель иногда шлёт короткое "ОК" → потом отдельное сообщение
                    # с tool-блоком. Подождём ещё 3с и перечитаем last_assistant; если текст
                    # изменился (новое сообщение) — продолжаем цикл.
                    await asyncio.sleep(3.0)
                    post_text = await read_last_assistant(self.page)
                    if post_text and post_text != last_text:
                        logger.info("[{}] После 'завершения' появился новый текст — продолжаем", self.profile_name)
                        last_text = post_text
                        last_change = time.time()
                        seen_activity = True
                        continue
                    logger.info("[{}] Генерация завершена.", self.profile_name)
                    with open("temp/final_generation_dump.txt", "w", encoding="utf-8") as f:
                        f.write(last_text)
                    completed_normally = True
                    break
                # ответ так и не появился — не висим
                if (not last_text or last_text == baseline) and (time.time() - start) > 25:
                    logger.warning("[{}] Ответ не появился за 25с — выходим.", self.profile_name)
                    break

                if time.time() - last_scroll >= random.uniform(1.5, 3.5):
                    await human_scroll(self.page)
                    last_scroll = time.time()
                await asyncio.sleep(interval)

            # Аномальное завершение цикла (таймаут или ответ не появился) — снимаем дамп DOM
            if not completed_normally:
                try:
                    import os as _os
                    _os.makedirs("temp", exist_ok=True)
                    ts = int(time.time())
                    html = await self.page.evaluate("document.documentElement.outerHTML")
                    dump_path = f"temp/timeout_dump_{self.profile_name.replace(' ', '_')}_{ts}.html"
                    with open(dump_path, "w", encoding="utf-8") as f:
                        f.write(html or "")
                    logger.warning("[{}] ⏱️ АНОМАЛЬНОЕ ЗАВЕРШЕНИЕ генерации (timeout={}, elapsed={:.1f}s). DOM dump: {}",
                                   self.profile_name, GENERATION_TIMEOUT, time.time() - start, dump_path)
                except Exception as de:
                    logger.error("[{}] Не удалось снять DOM dump: {}", self.profile_name, de)

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
