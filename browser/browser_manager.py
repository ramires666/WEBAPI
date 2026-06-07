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

import re as _re_bm


_WE_HEADER = _re_bm.compile(r'<<<(?:WRITE|EDIT)\b[^>]*>>>', _re_bm.IGNORECASE)

_LIVE_STATUS_RE = _re_bm.compile(
    r'<<<(WRITE|EDIT|READ|BASH|GLOB|GREP|FETCH|TODO|TASK|ASK|MSG)([^>]*)>>>',
    _re_bm.IGNORECASE,
)
_LIVE_PATH_RE = _re_bm.compile(r'path="([^"]*)"')
_LIVE_PATTERN_RE = _re_bm.compile(r'pattern="([^"]*)"')
# Частичный (ещё не закрытый '>>>') заголовок — модель только начала печатать '<<<VERB ...'
_LIVE_PARTIAL_RE = _re_bm.compile(
    r'<<<(WRITE|EDIT|READ|BASH|GLOB|GREP|FETCH|TODO|TASK|ASK|MSG)([^>]*)',
    _re_bm.IGNORECASE,
)
_LIVE_PATH_LOOSE_RE = _re_bm.compile(r'path="([^"\n]*)')
_LIVE_LABELS = {
    "WRITE": "📝 пишет", "EDIT": "✏️ правит", "READ": "📖 читает",
    "BASH": "🖥️ терминал", "GLOB": "🔍 поиск", "GREP": "🔎 grep",
    "FETCH": "🌐 запрос", "TODO": "📋 план", "TASK": "📋 задача",
    "ASK": "❓ вопрос", "MSG": "💬 пишет",
}


def _extract_live_status(text: str) -> str:
    """Из живого потока вынуть «что модель делает прямо сейчас» для пульса."""
    if not text:
        return ""
    matches = list(_LIVE_STATUS_RE.finditer(text))
    if matches:
        m = matches[-1]
        verb = m.group(1).upper()
        attrs = m.group(2) or ""
        label = _LIVE_LABELS.get(verb, verb)
        pm = _LIVE_PATH_RE.search(attrs)
        gm = _LIVE_PATTERN_RE.search(attrs)
        if pm:
            return f"{label} {pm.group(1).rsplit('/', 1)[-1]}"
        if gm:
            return f"{label} {gm.group(1)}"
        return label
    # полного заголовка нет — но мог начаться частичный '<<<VERB ...' (без '>>>').
    # Конвертим в чистый лейбл, чтобы сырой делимитер не мелькал текстом в Kilo.
    partials = list(_LIVE_PARTIAL_RE.finditer(text))
    if partials:
        pm = partials[-1]
        verb = pm.group(1).upper()
        attrs = pm.group(2) or ""
        label = _LIVE_LABELS.get(verb, verb)
        ppath = _LIVE_PATH_LOOSE_RE.search(attrs)
        if ppath and ppath.group(1).strip():
            return f"{label} {ppath.group(1).rsplit('/', 1)[-1]}"
        return label
    # заголовков нет — отдаём хвост видимого текста (преамбула/размышление),
    # срезая любой сырой '<<<' фрагмент
    for ln in reversed(text.splitlines()):
        cut = ln.find("<<<")
        if cut != -1:
            ln = ln[:cut]
        ln = ln.strip()
        if ln:
            return "💭 " + (ln[:50] + "…" if len(ln) > 50 else ln)
    return ""


# Полный заголовок делимитера — для определения границы живого стрима.
_STREAM_HEADER_RE = _re_bm.compile(
    r'<<<(WRITE|EDIT|READ|BASH|GLOB|GREP|FETCH|TODO|TASK|ASK|MSG)([^>]*)>>>',
    _re_bm.IGNORECASE,
)


def _stream_limit(text: str):
    """Докуда сырой текст безопасно стримить живьём — (limit, hard).

    MSG-тело — проза/markdown чата: стримим ЖИВЬЁМ (парсер срежет <<<MSG>>>/<<<END>>>).
    Любой не-MSG делимитер (WRITE/EDIT/код/тулзы) — ЖЁСТКИЙ стоп: limit на нём,
    hard=True → дальше атомарно из read_last_assistant (целостность кода > анимация).
      • limit — индекс, докуда можно отдать прямо сейчас;
      • hard  — True, если на limit подтверждённый не-MSG делимитер (стоп навсегда).
    Полные '<<<MSG>>>'/'<<<END>>>' проматываются; незакрытый хвостовой '<<<...'
    придерживается (hard=False) — может оказаться '<<<WRITE', ждём прояснения.
    """
    i = 0
    while True:
        j = text.find("<<<", i)
        if j == -1:
            return len(text), False
        seg = text[j:]
        m = _STREAM_HEADER_RE.match(seg)
        if m:
            if m.group(1).upper() == "MSG":
                i = j + m.end()          # MSG-заголовок проматываем — тело стримим
                continue
            return j, True               # WRITE/EDIT/... — жёсткий стоп
        if seg[:9].upper() == "<<<END>>>":
            i = j + 9                     # конец блока — проматываем
            continue
        return j, False                  # незакрытый '<<<...' — мягкая придержка

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
        self._current_model = None  # кэш выбранной модели: select_model — no-op если совпадает
        self._live_status = ""  # живой статус генерации для heartbeat (что модель делает сейчас)

    def get_live_status(self) -> str:
        """Текущий статус генерации (что модель делает прямо сейчас). Для heartbeat."""
        return self._live_status or ""

    def setup_profile(self):
        src = os.path.join(self.profiles_dir, self.profile_name)
        dst = os.path.join(self.work_dir, self.profile_name)
        if not os.path.exists(src):
            raise FileNotFoundError(f"[{self.profile_name}] Profile not found: {src}")
        logger.info("[{}] Синк профиля {} → {}", self.profile_name, src, dst)
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

    async def show_window(self):
        """Переместить окно браузера на экран (100,100) для ручного входа/просмотра."""
        if not self.page:
            return
        try:
            window_id, _ = await self.page.send(cdp_browser.get_window_for_target())
            await self.page.send(cdp_browser.set_window_bounds(
                window_id=window_id,
                bounds=cdp_browser.Bounds(
                    left=100, top=100, width=1280, height=900,
                    window_state=cdp_browser.WindowState.NORMAL,
                )
            ))
            logger.info("[{}] Окно выведено на экран", self.profile_name)
        except Exception as e:
            logger.warning("[{}] show_window failed: {}", self.profile_name, e)

    async def hide_window(self):
        """Убрать окно браузера за экран (-32000,-32000)."""
        if not self.page:
            return
        try:
            window_id, _ = await self.page.send(cdp_browser.get_window_for_target())
            await self.page.send(cdp_browser.set_window_bounds(
                window_id=window_id,
                bounds=cdp_browser.Bounds(
                    left=-32000, top=-32000, width=1280, height=900,
                    window_state=cdp_browser.WindowState.NORMAL,
                )
            ))
            logger.info("[{}] Окно убрано off-screen", self.profile_name)
        except Exception as e:
            logger.warning("[{}] hide_window failed: {}", self.profile_name, e)

    async def get_current_url(self) -> str:
        return current_url(self.page)

    async def select_model(self, target_model: str):
        """Переключение модели на новом ChatGPT UI (Radix dropdown).
        Триггер = button[aria-haspopup="menu"] с текстом-моделью или aria-label
        'switch model'. Пункты = div[role="menuitemradio"][data-testid^="model-switcher-"];
        текущая модель = пункт с aria-checked="true". Открытие — РЕАЛЬНЫЙ mouse_click
        (click_element), иначе Radix не открывается. Кэш self._current_model — no-op
        при совпадении (после первого успешного выбора в чате повторы мгновенны)."""
        target_low = target_model.strip().lower()
        if self._current_model == target_low:
            return  # уже на этой модели — мгновенный no-op

        SKIP_TID = ("history-item", "accounts-profile", "composer-plus")
        SKIP_AL = ("conversation", "sidebar", "profile", "download", "add files",
                   "dictation", "switch model", "more actions")
        MODEL_KW = ("instant", "thinking", "auto", "extended", "standard", "legacy", "pro", "gpt")

        # --- найти кнопку-триггер ---
        trigger = None
        try:
            buttons = await self.page.select_all("button")
        except Exception:
            buttons = []
        for b in buttons or []:
            try:
                if (b.attrs.get("aria-haspopup") or "") != "menu":
                    continue
                tid = (b.attrs.get("data-testid") or "").lower()
                al = (b.attrs.get("aria-label") or "").lower()
                txt = (b.text_all or "").strip().lower()
                if any(s in tid for s in SKIP_TID) or any(s in al for s in SKIP_AL):
                    continue
                # Композерная кнопка модели = единственная button[aria-haspopup=menu]
                # с ВИДИМЫМ текстом модели (Instant/Thinking/Extended/Auto). НЕЛЬЗЯ
                # матчить по aria-label 'model': на странице чата у каждого сообщения
                # есть инлайновая 'Switch model', часто проскроленная за экран (y<0) —
                # реальный клик по ней висит ~2 мин и меню не открывается.
                if any(txt.startswith(k) for k in MODEL_KW):
                    trigger = b
                    break
            except Exception:
                continue
        if trigger is None:
            logger.warning("[{}] select_model: кнопка-триггер модели не найдена (target='{}')",
                           self.profile_name, target_model)
            return

        # --- открыть меню реальным кликом ---
        if not await click_element(trigger):
            logger.warning("[{}] select_model: не удалось кликнуть триггер", self.profile_name)
            return

        # --- дождаться пунктов меню ---
        items = []
        for _ in range(12):  # до ~2.4с
            await asyncio.sleep(0.2)
            try:
                raw = await self.page.select_all('[role="menuitemradio"]')
            except Exception:
                raw = []
            items = [it for it in (raw or [])
                     if (it.attrs.get("data-testid") or "").startswith("model-switcher-")]
            if items:
                break
        if not items:
            logger.warning("[{}] select_model: меню открылось, но пунктов модели нет", self.profile_name)
            try:
                await send_key(self.page, "Escape", 27)
            except Exception:
                pass
            return

        # --- разобрать пункты: текущий (aria-checked) + целевой ---
        target_item = None
        current_text = ""
        for it in items:
            try:
                itxt = (it.text_all or "").strip()
                if (it.attrs.get("aria-checked") or "").lower() == "true":
                    current_text = itxt
                if itxt.lower().startswith(target_low):
                    target_item = it
            except Exception:
                continue
        logger.info("[{}] select_model: текущая='{}', target='{}', пунктов={}",
                    self.profile_name, current_text, target_model, len(items))

        # уже на нужной модели — закрыть меню и закэшировать
        if current_text.lower().startswith(target_low):
            try:
                await send_key(self.page, "Escape", 27)
            except Exception:
                pass
            self._current_model = target_low
            return

        if target_item is None:
            logger.warning("[{}] select_model: пункт '{}' не найден (есть: {})",
                           self.profile_name, target_model,
                           [(it.text_all or "").strip()[:20] for it in items])
            try:
                await send_key(self.page, "Escape", 27)
            except Exception:
                pass
            return

        # --- кликнуть целевой пункт ---
        if await click_element(target_item):
            self._current_model = target_low
            logger.info("[{}] ✅ select_model: переключено на '{}'", self.profile_name, target_model)
            await asyncio.sleep(0.3)
        else:
            logger.warning("[{}] select_model: клик по пункту '{}' не удался", self.profile_name, target_model)

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

    async def send_prompt_and_stream(self, prompt_text: str, target_model: str, is_new_chat: bool, chat_url: str = None, typed_segment: str = ""):
        async with self._lock:
            self._live_status = ""  # сброс статуса от прошлого запроса
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
                self._current_model = None  # новый чат может сбросить модель аккаунта — форс ре-селект
                logger.info("[{}] === НАЧАЛО НОВОГО ЧАТА ===", self.profile_name)
                try:
                    await asyncio.wait_for(self.page.get(CHATGPT_URL), timeout=20.0)
                except asyncio.TimeoutError:
                    logger.warning("[{}] ⏱️ page.get(new chat) таймаут 20с — окно свёрнуто/throttled? bring_to_front", self.profile_name)
                    try:
                        await asyncio.wait_for(self.page.bring_to_front(), timeout=5.0)
                    except Exception:
                        pass
                await asyncio.sleep(3.5)
                await self._apply_focus_emulation()
            elif chat_url:
                current_url = await self.get_current_url()
                if current_url.rstrip("/") != chat_url.rstrip("/"):
                    logger.info("[{}] === ПЕРЕХОД В ЧАТ {} ===", self.profile_name, chat_url)
                    try:
                        await asyncio.wait_for(self.page.get(chat_url), timeout=20.0)
                    except asyncio.TimeoutError:
                        logger.warning("[{}] ⏱️ page.get(chat) таймаут 20с — окно свёрнуто/throttled? bring_to_front", self.profile_name)
                        try:
                            await asyncio.wait_for(self.page.bring_to_front(), timeout=5.0)
                        except Exception:
                            pass
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
            await asyncio.sleep(0.15)
            # Гибрид-ввод: систему/контекст/результаты тулзов ПАСТИМ (insert_text, мгновенно),
            # а смысловой текст юзера ВПЕЧАТЫВАЕМ реальными key-событиями (человеко-ввод) на МАКС скорости.
            seg = typed_segment.strip() if typed_segment else ""
            idx = prompt_text.find(seg) if seg else -1
            if seg and idx != -1:
                pre = prompt_text[:idx]
                post = prompt_text[idx + len(seg):]
                logger.info("[{}] Гибрид-ввод: паста {} + впечатка {} + паста {} симв",
                            self.profile_name, len(pre), len(seg), len(post))
                if pre:
                    await insert_text_fast(self.page, pre)
                try:
                    await textarea.send_keys(seg)
                except Exception as _te:
                    logger.debug("[{}] впечатка не удалась, фоллбэк паста: {}", self.profile_name, _te)
                    await insert_text_fast(self.page, seg)
                if post:
                    await insert_text_fast(self.page, post)
            else:
                logger.info("[{}] Паста всего промпта ({} симв)...", self.profile_name, len(prompt_text))
                await insert_text_fast(self.page, prompt_text)
            await asyncio.sleep(0.1)
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

            # Верификация: если через 0.4с textarea всё ещё содержит наш промпт
            # (или его начало), значит submit не сработал — досылаем Enter.
            await asyncio.sleep(0.4)
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
            delim_seen = False   # True после первого <<<делимитера>>> — дальше прозу не стримим

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
                    try:
                        self._live_status = _extract_live_status(current_text)
                    except Exception:
                        pass

                # ЖИВОЙ СТРИМ ПРОЗЫ + ЧАТА (MSG). Лидирующая проза и тело <<<MSG>>>
                # (markdown ответа в чат) — отдаём живьём, чтобы Kilo рисовал по мере
                # генерации; парсер срежет маркеры <<<MSG>>>/<<<END>>>. Первый НЕ-MSG
                # делимитер (WRITE/EDIT/код/тулзы) — стоп: досылаем атомарно из
                # авторитетного read_last_assistant в финале (байт-в-байт, без риска
                # порчи кода и дублей tool_calls). Инвариант: emitted всегда точный
                # префикс будущего финала → конкатенация чанков == финал.
                if not started and current_text and current_text != baseline:
                    started = True
                if not delim_seen and current_text != baseline and current_text.startswith(emitted):
                    _hard_limit, _is_hard = _stream_limit(current_text)
                    _stable = _cpl(prev_text, current_text)   # префикс, стабильный в 2 чтениях
                    _limit = min(_hard_limit, _stable)
                    if _is_hard and _stable >= _hard_limit:
                        delim_seen = True                     # дошли до WRITE/EDIT — дальше атомарно
                    _chunk = current_text[len(emitted):_limit]
                    if _chunk:
                        emitted += _chunk
                        if t_first_chunk is None:
                            t_first_chunk = time.perf_counter()
                        yield _chunk
                prev_text = current_text

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
                if last_text and last_text != baseline and not stop_present and (time.time() - last_change) >= 1.5:
                    # Poll 0.5с: ловим зазор между "ОК" и вторым tool-call сообщением.
                    # Фиксированный sleep(N) ненадёжен — зазор может быть > N.
                    # Poll выходит немедленно при любой активности (stop вернулся / текст изменился).
                    _poll_done = True
                    _poll_end = time.time() + 0.5
                    while time.time() < _poll_end:
                        await asyncio.sleep(0.1)
                        try:
                            _sp2 = bool(await asyncio.wait_for(self.page.evaluate(
                                'document.querySelector(\'button[aria-label="Stop generating"], button[data-testid="stop-button"]\') ? true : false'
                            ), timeout=3.0))
                        except Exception:
                            _sp2 = False
                        if _sp2:
                            _poll_done = False
                            seen_activity = True
                            last_change = time.time()
                            break
                        try:
                            _new = await asyncio.wait_for(read_stream_buffer(self.page), timeout=5.0)
                        except Exception:
                            _new = last_text
                        if _new != last_text:
                            last_text = _new
                            last_change = time.time()
                            seen_activity = True
                            _poll_done = False
                            break
                    if not _poll_done:
                        continue
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

                # Человеко-следование за растущим ответом: держим низ в виду (особенно
                # для длинных ответов). Решительный скролл вниз + редкий взгляд вверх.
                if time.time() - last_scroll >= random.uniform(0.8, 1.6):
                    try:
                        if random.random() < 0.12:
                            await self.page.scroll_up(random.randint(30, 80))
                        else:
                            await self.page.scroll_down(random.randint(280, 520))
                    except Exception:
                        pass
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
                if t_first_chunk is None:
                    t_first_chunk = time.perf_counter()
                if final_text.startswith(emitted):
                    tail = final_text[len(emitted):]
                else:
                    _k = _cpl(emitted, final_text)
                    tail = final_text[_k:]
                    logger.warning("[{}] ⚠ стрим разошёлся с финалом на {}/{} — досыл общего хвоста",
                                   self.profile_name, _k, len(emitted))
                logger.info("[{}] 📤 yield: streamed={}, tail={}, total_final={}",
                            self.profile_name, len(emitted), len(tail), len(final_text))
                if tail:
                    yield tail
            t_post_start = time.perf_counter()
            # click_download_buttons + collect_files синхронно — blob нужен клиенту, и быстро при 0 кликах.
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

            # scroll_bottom + popup — в фоне, чтобы генератор вернулся немедленно:
            # клиент получит [DONE] без ожидания браузерной косметики.
            _self = self
            async def _bg_post():
                try:
                    await asyncio.wait_for(scroll_bottom(_self.page), timeout=2.0)
                    logger.info("[{}] ⏱ bg scroll_bottom done", _self.profile_name)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(_self._handle_personality_popup(), timeout=0.3)
                except Exception:
                    pass
            asyncio.create_task(_bg_post())

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
