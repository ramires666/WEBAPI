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
    CHATGPT_URL, GENERATION_TIMEOUT, TEMP_DOWNLOADS, NO_MINIMIZE_PROFILES,
)
from browser.cdp_native import (
    send_key, human_type, insert_text_fast, click_element, find_element,
    scroll_bottom, human_scroll, current_url, read_last_assistant,
    inject_stream_observer, read_stream_buffer,
)
from browser.file_extractor import click_download_buttons, collect_files

warnings.filterwarnings('ignore', category=ResourceWarning)

STOP_SELECTOR = 'button[aria-label="Stop generating"], button[data-testid="stop-button"]'
SEND_SELECTOR = '[data-testid="send-button"], button[data-testid="composer-send-button"]'
COMPOSER_SELECTOR = "#prompt-textarea"
# JS shim, injected on every navigation via Page.addScriptToEvaluateOnNewDocument.
# Forces the page to act as if it's always focused+visible — bypasses Chrome's
# OS-level focus throttling that even --disable-renderer-backgrounding can't fully kill.
FOCUS_SHIM_JS = """
(() => {
  try {
    Object.defineProperty(document, 'visibilityState', {get: () => 'visible', configurable: true});
    Object.defineProperty(document, 'hidden', {get: () => false, configurable: true});
    Object.defineProperty(document, 'webkitVisibilityState', {get: () => 'visible', configurable: true});
    Object.defineProperty(document, 'webkitHidden', {get: () => false, configurable: true});
    document.hasFocus = () => true;
    window.addEventListener('visibilitychange', e => { e.stopImmediatePropagation(); }, true);
    window.addEventListener('webkitvisibilitychange', e => { e.stopImmediatePropagation(); }, true);
    window.addEventListener('blur', e => { e.stopImmediatePropagation(); }, true);
  } catch (e) { /* swallow */ }
})();
"""
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
        """Применить CDP-эмуляцию фокуса + JS-шим visibility/hasFocus.
        Вызывать ПОСЛЕ каждой навигации (page.get), иначе настройка теряется
        и ChatGPT начинает throttle'ить фоновые вкладки."""
        try:
            await self.page.send(cdp_emulation.set_focus_emulation_enabled(enabled=True))
        except Exception as e:
            logger.warning("[{}] Focus emulation failed: {}", self.profile_name, e)
        try:
            await self.page.send(cdp_page.set_web_lifecycle_state(state="active"))
        except Exception as e:
            logger.warning("[{}] Lifecycle set failed: {}", self.profile_name, e)
        # JS-шим: переопределяет visibilityState/hidden/hasFocus, гасит
        # visibilitychange/blur — страницa "не знает", что вкладка фоновая.
        try:
            await self.page.send(cdp_page.add_script_to_evaluate_on_new_document(source=FOCUS_SHIM_JS))
        except Exception as e:
            logger.warning("[{}] add_script_to_evaluate_on_new_document failed: {}", self.profile_name, e)
        # Применить шим к УЖЕ загруженной странице (на новый документ — addScript, но текущий уже отрендерен).
        try:
            await asyncio.wait_for(self.page.evaluate(FOCUS_SHIM_JS), timeout=3.0)
        except Exception as e:
            logger.debug("[{}] focus shim eval failed: {}", self.profile_name, e)

    async def start_browser(self):
        self.setup_profile()
        config = uc.Config()
        config.user_data_dir = os.path.join(self.work_dir, self.profile_name)
        config.add_argument("--profile-directory=Default")
        # Анти-throttle: Chrome не должен замораживать таймеры/рендер в фоновых
        # окнах (CDP setFocusEmulationEnabled не покрывает process-level
        # backgrounding — нужны явные флаги запуска).
        config.add_argument("--disable-background-timer-throttling")
        config.add_argument("--disable-backgrounding-occluded-windows")
        config.add_argument("--disable-renderer-backgrounding")
        self.browser = await uc.start(config)
        self.page = await self.browser.get(CHATGPT_URL)

        # Унести окно за пределы экрана — рендерит DOM, CDP работает, но не видно.
        # MINIMIZED ломает: пункты меню не рендерятся, ChatGPT select_model/ввод глохнут.
        # Профили из NO_MINIMIZE_PROFILES остаются на экране (визуальный контроль).
        if self.profile_name not in NO_MINIMIZE_PROFILES:
            try:
                window_id, _ = await self.page.send(cdp_browser.get_window_for_target())
                await self.page.send(cdp_browser.set_window_bounds(
                    window_id=window_id,
                    bounds=cdp_browser.Bounds(
                        left=-32000, top=-32000, width=1280, height=900,
                        window_state=cdp_browser.WindowState.NORMAL,
                    )
                ))
                logger.info("[{}] Окно уведено off-screen (-32000,-32000)", self.profile_name)
            except Exception as e:
                logger.warning("[{}] Не удалось увести окно off-screen: {}", self.profile_name, e)
        else:
            logger.info("[{}] Окно остаётся на экране (в NO_MINIMIZE_PROFILES)", self.profile_name)

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
        обычно button[aria-haspopup="menu"], чей текст содержит модель
        (Instant/Thinking/Auto). Фоллбэк — поиск по data-testid/aria-label
        (на некоторых аккаунтах текст кнопки пустой и её можно опознать
        только по этим атрибутам)."""
        target_low = target_model.strip().lower()
        timeout_sec = 8.0
        interval_sec = 0.3
        start_time = time.perf_counter()
        selector_btn = None
        current_raw = ""
        match_strategy = ""
        candidates = []
        attempt_count = 0

        def _attr(b, name):
            try:
                v = b.attrs.get(name)
                return (v or "").lower() if isinstance(v, str) else ""
            except Exception:
                return ""

        while time.perf_counter() - start_time < timeout_sec:
            attempt_count += 1
            try:
                buttons = await self.page.select_all("button")
            except Exception:
                buttons = []

            candidates = []
            for b in buttons or []:
                try:
                    if b.attrs.get("aria-haspopup") == "menu":
                        candidates.append((b, (b.text_all or "").strip()))
                except Exception:
                    continue

            # Стратегия 1: aria-haspopup="menu" + текст содержит instant/thinking/auto
            for b, raw in candidates:
                tl = raw.lower()
                if any(k in tl for k in ("instant", "thinking", "auto")) and "project" not in tl and "share" not in tl:
                    selector_btn = b
                    current_raw = raw
                    match_strategy = "haspopup+text"
                    break

            # Стратегия 2: aria-haspopup="menu" + aria-label/data-testid содержит model/модел
            if not selector_btn:
                for b, raw in candidates:
                    al = _attr(b, "aria-label")
                    tid = _attr(b, "data-testid")
                    if ("model" in al or "модел" in al or "model" in tid):
                        selector_btn = b
                        current_raw = raw or al or tid
                        match_strategy = f"haspopup+attr(al='{al[:30]}',tid='{tid[:30]}')"
                        break

            # Стратегия 3: любая кнопка с data-testid*="model-switcher" / aria-label model selector
            if not selector_btn:
                try:
                    extra = await self.page.select_all(
                        '[data-testid*="model"], [aria-label*="model" i], [aria-label*="Model"]'
                    )
                except Exception:
                    extra = []
                for b in extra or []:
                    try:
                        al = _attr(b, "aria-label")
                        tid = _attr(b, "data-testid")
                        if "switcher" in tid or "switch" in al or "select model" in al or "model" == al.strip() or "modell" in al:
                            selector_btn = b
                            current_raw = (b.text_all or "").strip() or al or tid
                            match_strategy = f"attr-only(al='{al[:30]}',tid='{tid[:30]}')"
                            break
                        # любой результат селектора, у которого testid начинается с model-
                        if tid.startswith("model-"):
                            selector_btn = b
                            current_raw = (b.text_all or "").strip() or al or tid
                            match_strategy = f"testid-prefix(tid='{tid[:40]}')"
                            break
                    except Exception:
                        continue

            # Стратегия 4 (Atlas-аккаунты): aria-haspopup="menu" кнопка с текстом-режимом
            # вроде 'Extended' / 'Standard' (не из чёрного списка sidebar/history/composer).
            if not selector_btn:
                BLACKLIST_TEXTS = {"recents", "projects", "search chats", ""}
                BLACKLIST_AL = ("open conversation", "open sidebar", "close sidebar",
                                "download apps", "add files", "composer", "send prompt",
                                "start dictation", "turn on temporary")
                for b, raw in candidates:
                    try:
                        al = _attr(b, "aria-label")
                        tid = _attr(b, "data-testid")
                        tlow = raw.lower().strip()
                        if tlow in BLACKLIST_TEXTS:
                            continue
                        if any(bad in al for bad in BLACKLIST_AL):
                            continue
                        if "history-item" in tid or "composer-plus" in tid:
                            continue
                        # Это режим-кнопка Atlas-UI (Extended и т.п.) — берём её
                        selector_btn = b
                        current_raw = raw
                        match_strategy = f"atlas-mode-btn(raw='{raw[:30]}')"
                        break
                    except Exception:
                        continue

            if selector_btn:
                break

            await asyncio.sleep(interval_sec)

        logger.info("[{}] КНОПКИ-МЕНЮ aria-haspopup (всего {}, попыток={}, стратегия='{}'): {}",
                    self.profile_name, len(candidates), attempt_count, match_strategy or "—",
                    [t[:60] for _, t in candidates])

        if not selector_btn:
            # ДИАГНОСТИЧЕСКИЙ ДАМП: что вообще на странице?
            try:
                url_now = current_url(self.page)
            except Exception:
                url_now = "?"
            try:
                title_node = await self.page.evaluate("document.title")
                page_title = str(title_node)[:120] if title_node else ""
            except Exception:
                page_title = "?"
            # Все кнопки + aria-label первых 30
            try:
                all_btns = await self.page.select_all("button")
            except Exception:
                all_btns = []
            btn_dump = []
            for b in (all_btns or [])[:30]:
                try:
                    al = _attr(b, "aria-label")
                    tid = _attr(b, "data-testid")
                    hp = _attr(b, "aria-haspopup")
                    txt = (b.text_all or "").strip()[:30]
                    btn_dump.append(f"al='{al[:25]}' tid='{tid[:25]}' hp='{hp}' txt='{txt}'")
                except Exception:
                    continue
            logger.warning("[{}] Кнопка выбора модели не найдена (target='{}'). URL='{}', title='{}'. Всего button={}. Дамп первых 30:",
                           self.profile_name, target_model, url_now, page_title, len(all_btns or []))
            for i, row in enumerate(btn_dump):
                logger.warning("[{}]   btn[{}]: {}", self.profile_name, i, row)
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
                    clicked_item_text = (it.text_all or "").strip()
                    click_ok = await click_element(it)
                    await asyncio.sleep(1.2)
                    # ВЕРИФИКАЦИЯ: перечитываем кнопку (включая Atlas-режимы)
                    try:
                        buttons2 = await self.page.select_all("button")
                        verified_raw = ""
                        for b2 in buttons2 or []:
                            try:
                                if b2.attrs.get("aria-haspopup") == "menu":
                                    txt2 = (b2.text_all or "").strip()
                                    if any(k in txt2.lower() for k in ("instant", "thinking", "auto", "extended", "standard")):
                                        verified_raw = txt2
                                        break
                            except Exception:
                                continue
                        if target_low in verified_raw.lower():
                            logger.info("[{}] ✅ ВЕРИФИКАЦИЯ: модель переключена на '{}' (raw='{}')",
                                        self.profile_name, target_model, verified_raw)
                        elif click_ok and target_low in clicked_item_text.lower():
                            # Косвенная верификация: клик прошёл по правильному пункту,
                            # но Atlas-UI не показывает target в тексте кнопки (рендерит 'Extended' и т.п.)
                            logger.info("[{}] ✅ ВЕРИФИКАЦИЯ (косвенная, Atlas-UI): клик по пункту '{}' выполнен, кнопка raw='{}'",
                                        self.profile_name, clicked_item_text, verified_raw or "—")
                        else:
                            logger.warning("[{}] ⚠️ ВЕРИФИКАЦИЯ ПРОВАЛЕНА: target='{}', получено raw='{}', clicked_item='{}'",
                                           self.profile_name, target_model, verified_raw, clicked_item_text)
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
            # HEALTH CHECK: probe CDP — if tab is frozen/dead, reload before anything else.
            try:
                await asyncio.wait_for(self.page.evaluate("1"), timeout=5.0)
            except Exception as _hc_err:
                logger.warning("[{}] 🚨 TAB UNHEALTHY ({}) — пробую reload", self.profile_name, _hc_err)
                try:
                    await asyncio.wait_for(self.page.reload(), timeout=20.0)
                    await asyncio.sleep(3.0)
                    await self._apply_focus_emulation()
                    logger.info("[{}] ✅ tab reload OK", self.profile_name)
                except Exception as _rl_err:
                    logger.error("[{}] 🚨 RELOAD FAILED ({}) — abort", self.profile_name, _rl_err)
                    yield f"Error: tab dead and reload failed: {_rl_err}"
                    return

            # BUG FIX 1: wake the tab regardless of branch — prevents background-throttle stalls
            try:
                await asyncio.wait_for(self.page.bring_to_front(), timeout=5.0)
            except Exception as _bt_err:
                logger.debug("[{}] bring_to_front failed: {}", self.profile_name, _bt_err)

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

            # BUG FIX 2: ensure textarea is empty before typing — Chrome may restore stale composer text
            try:
                _ta = await find_element(self.page, COMPOSER_SELECTOR, timeout=5)
                _ta_text = (_ta.text_all or "").strip() if _ta else ""
                if _ta and _ta_text:
                    logger.warning("[{}] 🧹 Стейл-текст в textarea ({} симв): чистим", self.profile_name, len(_ta_text))
                    # triple-click selects all in contenteditable
                    for _ in range(3):
                        await click_element(_ta)
                        await asyncio.sleep(0.05)
                    await send_key(self.page, "Delete", 46)
                    await asyncio.sleep(0.2)
            except Exception as _cl_err:
                logger.debug("[{}] textarea clear skipped: {}", self.profile_name, _cl_err)

            # Финальная верификация режима перед отправкой
            try:
                btns_check = await self.page.select_all("button")
                mode_btn_text = ""
                for bc in btns_check or []:
                    try:
                        if bc.attrs.get("aria-haspopup") == "menu":
                            tc = (bc.text_all or "").strip()
                            if any(k in tc.lower() for k in ("instant", "thinking", "auto", "extended", "standard")):
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
            # Всегда нативная вставка (insert_text) — human_type слишком медленный для инжектов.
            logger.info("[{}] Нативная вставка insert_text ({} симв)...", self.profile_name, len(prompt_text))
            await insert_text_fast(self.page, prompt_text)
            await asyncio.sleep(0.5)

            await asyncio.sleep(0.8)
            send_btn = await find_element(self.page, SEND_SELECTOR, timeout=3)

            # Ждём пока кнопка станет enabled (composer на доли секунды дизейблит
            # её после вставки текста — клик по disabled "проходит", но не шлёт).
            if send_btn:
                for _ in range(20):  # до ~2с
                    try:
                        dis = send_btn.attrs.get("disabled")
                        aria_dis = send_btn.attrs.get("aria-disabled")
                        if not dis and aria_dis not in ("true", True):
                            break
                    except Exception:
                        break
                    await asyncio.sleep(0.1)
                    try:
                        send_btn = await find_element(self.page, SEND_SELECTOR, timeout=0.5)
                        if not send_btn:
                            break
                    except Exception:
                        break
                try:
                    dis_final = send_btn.attrs.get("disabled") if send_btn else "no-btn"
                    aria_final = send_btn.attrs.get("aria-disabled") if send_btn else "no-btn"
                    logger.info("[{}] 🔘 send state: disabled={}, aria-disabled={}",
                                self.profile_name, dis_final, aria_final)
                except Exception:
                    pass

            click_ok = await click_element(send_btn) if send_btn else False
            if not click_ok:
                logger.info("[{}] 🔁 SUBMIT FALLBACK Enter (нет кнопки или клик не удался)", self.profile_name)
                await send_key(self.page, "Enter", 13)

            # Верификация: если через 0.7с textarea всё ещё содержит наш промпт
            # (или его начало), значит submit не сработал — досылаем Enter.
            await asyncio.sleep(0.7)
            try:
                ta_now = await find_element(self.page, COMPOSER_SELECTOR, timeout=0.5)
                ta_text = (ta_now.text_all or "") if ta_now else ""
                # Берём первые 40 символов промпта как маркер
                marker = prompt_text[:40].strip()
                if marker and marker in ta_text:
                    logger.warning("[{}] 🔁 SUBMIT FALLBACK Enter (промпт всё ещё в textarea: {!r})",
                                   self.profile_name, ta_text[:60])
                    if ta_now:
                        await click_element(ta_now)
                        await asyncio.sleep(0.2)
                    await send_key(self.page, "Enter", 13)
            except Exception as ve:
                logger.debug("[{}] verify-submit failed: {}", self.profile_name, ve)

            logger.info("[{}] Запрос отправлен. Ждём генерацию...", self.profile_name)
            await inject_stream_observer(self.page)  # обсервер: дешёвое чтение window.__pxFull
            interval = 0.05  # Быстрый поллинг (чтение копеечное — обсервер уже посчитал)
            start = time.time()
            t_submit = time.perf_counter()
            t_first_chunk = None
            t_done = None
            last_change = start
            last_scroll = start
            last_focus_refresh = start
            seen_activity = False
            try:
                baseline = await asyncio.wait_for(read_last_assistant(self.page), timeout=15.0)
            except asyncio.TimeoutError:
                logger.warning("[{}] baseline read_last_assistant timeout 15s — using empty baseline", self.profile_name)
                baseline = ""
            last_text = baseline
            prev_text = baseline
            # emitted: префикс НОВОГО ответа, уже отданный клиенту (стрим по 2 чтениям)
            emitted = ""
            started = False

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
                    stop_present = bool(await asyncio.wait_for(self.page.evaluate(
                        'document.querySelector(\'button[aria-label="Stop generating"], button[data-testid="stop-button"]\') ? true : false'
                    ), timeout=5.0))
                except Exception:
                    pass

                try:
                    current_text = await asyncio.wait_for(read_stream_buffer(self.page), timeout=10.0)
                except asyncio.TimeoutError:
                    logger.warning("[{}] read_stream_buffer таймаут 10s — bring_to_front + skip iter", self.profile_name)
                    try:
                        await asyncio.wait_for(self.page.bring_to_front(), timeout=3.0)
                    except Exception:
                        pass
                    await asyncio.sleep(interval)
                    continue
                if current_text != last_text:
                    last_text = current_text
                    seen_activity = True
                    last_change = time.time()

                # новый ответ начался (отличается от того, что было до отправки)
                if not started and current_text and current_text != baseline:
                    started = True
                    # prev_text оставляем = baseline → первый _cpl даст 0,
                    # первый чанк подтвердится ВТОРЫМ чтением (анти-транзиент).

                # СТАБИЛЬНЫЙ СТРИМИНГ: отдаём только префикс, совпавший в ДВУХ
                # подряд чтениях DOM (отсекает транзиентные ре-рендеры markdown,
                # плющащие маркер-строки) и только как чистое продолжение emitted.
                if started:
                    sp = _cpl(prev_text, current_text)
                    prev_text = current_text
                    if sp > len(emitted) and current_text[:len(emitted)] == emitted:
                        chunk = current_text[len(emitted):sp]
                        emitted = current_text[:sp]
                        if chunk:
                            if t_first_chunk is None:
                                t_first_chunk = time.perf_counter()
                            yield chunk

                if stop_present:
                    seen_activity = True
                    last_change = time.time()

                # Периодический refresh CDP-эмуляции фокуса (раз в 1.5 секунды)
                if time.time() - last_focus_refresh >= 1.5:
                    try:
                        await asyncio.wait_for(self._apply_focus_emulation(), timeout=4.0)
                    except asyncio.TimeoutError:
                        logger.warning("[{}] focus refresh таймаут 4s", self.profile_name)
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
                    t_done = time.perf_counter()
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

            # Досыл хвоста: берём АВТОРИТЕТНЫЙ финал свежим read_last_assistant
            # (обсервер мог отстать на одну мутацию на самом конце).
            try:
                authoritative = await asyncio.wait_for(read_last_assistant(self.page), timeout=10.0)
            except Exception:
                authoritative = last_text
            final_text = authoritative if (authoritative and authoritative != baseline) else (last_text if last_text != baseline else "")
            logger.info("[{}] 📝 FINAL_TEXT: len={}, has_write={}, has_end={}, has_edit={}, head={!r}",
                        self.profile_name, len(final_text),
                        "<<<WRITE" in final_text, "<<<END>>>" in final_text, "<<<EDIT" in final_text,
                        final_text[:200])
            if final_text:
                # Инвариант: emitted — точный префикс финала. Если транзиент его
                # нарушил — _cpl-фоллбэк (досылаем с точки расхождения).
                if final_text.startswith(emitted):
                    tail = final_text[len(emitted):]
                else:
                    cp = _cpl(emitted, final_text)
                    logger.warning("[{}] ⚠️ emitted разошёлся с финалом (cp={}, emitted={}) — _cpl-фоллбэк",
                                   self.profile_name, cp, len(emitted))
                    tail = final_text[cp:]
                if tail:
                    logger.info("[{}] 📤 yield tail: len={}", self.profile_name, len(tail))
                    yield tail
            t_post_start = time.perf_counter()
            t_scroll_start = time.perf_counter()
            try:
                await asyncio.wait_for(scroll_bottom(self.page), timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("[{}] scroll_bottom таймаут 2s — пропускаем", self.profile_name)
            except Exception as e:
                logger.warning("[{}] scroll_bottom ошибка: {}", self.profile_name, e)
            logger.info("[{}] ⏱ scroll_bottom: {:.2f}s", self.profile_name, time.perf_counter() - t_scroll_start)
            await asyncio.sleep(0.3)
            t_click_start = time.perf_counter()
            clicked = await click_download_buttons(self.page)
            logger.info("[{}] ⏱ click_download_buttons: {:.2f}s, clicked={}",
                        self.profile_name, time.perf_counter() - t_click_start, clicked)
            if clicked > 0:
                await asyncio.sleep(2.0)
            t_collect_start = time.perf_counter()
            blob = await collect_files(self.page)
            logger.info("[{}] ⏱ collect_files: {:.2f}s, blob_len={}",
                        self.profile_name, time.perf_counter() - t_collect_start, len(blob or ""))
            if blob:
                yield blob

            t_popup_start = time.perf_counter()
            try:
                await asyncio.wait_for(self._handle_personality_popup(), timeout=1.0)
            except asyncio.TimeoutError:
                logger.warning("[{}] _handle_personality_popup таймаут 1s — пропускаем", self.profile_name)
            logger.info("[{}] ⏱ popup: {:.2f}s", self.profile_name, time.perf_counter() - t_popup_start)

            # Сводка таймингов
            t_end = time.perf_counter()
            submit_to_first = (t_first_chunk - t_submit) if t_first_chunk else None
            gen_time = (t_done - t_submit) if t_done else None
            post_time = t_end - t_post_start
            total = t_end - t_submit
            logger.info("[{}] 📊 ТАЙМИНГ: submit→first={}, gen={}, post={:.2f}s, total={:.2f}s",
                        self.profile_name,
                        f"{submit_to_first:.2f}s" if submit_to_first else "N/A",
                        f"{gen_time:.2f}s" if gen_time else "N/A",
                        post_time, total)
