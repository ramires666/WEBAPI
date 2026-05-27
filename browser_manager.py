import asyncio
import random
import os
import shutil
import warnings
from loguru import logger
import nodriver as uc
import nodriver.cdp.input_ as cdp_input
import nodriver.cdp.browser as cdp_browser

# Suppress warnings
warnings.filterwarnings('ignore', category=ResourceWarning)

ORIGINAL_CHROME_USER_DATA = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
WORKING_PROFILE_DIR = r"W:\_python\APIPROXY\chrome_work_profile"
CHROME_PROFILE_NAME = "Profile 2"
CHATGPT_URL = "https://chatgpt.com"
GENERATION_TIMEOUT = 300
TEMP_DOWNLOADS = r"W:\_python\APIPROXY\temp_downloads"
TYPING_DELAY = (0.01, 0.05)

async def send_key(page, key_name: str, key_code: int):
    await page.send(cdp_input.dispatch_key_event(
        type_="keyDown",
        key=key_name,
        windows_virtual_key_code=key_code,
        native_virtual_key_code=key_code
    ))
    await asyncio.sleep(0.05)
    await page.send(cdp_input.dispatch_key_event(
        type_="keyUp",
        key=key_name,
        windows_virtual_key_code=key_code,
        native_virtual_key_code=key_code
    ))

async def human_type(element, text: str) -> None:
    for char in text:
        await element.send_keys(char)
        await asyncio.sleep(random.uniform(*TYPING_DELAY))

async def find_element(page, selector: str, *, timeout: float = 10.0, interval: float = 0.3):
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
            
    async def select_model(self, target_model: str):
        logger.info("Поиск кнопки модели в композиторе...")
        btn_text = await self.page.evaluate("""
            (() => {
                const textarea = document.querySelector("#prompt-textarea");
                if (!textarea) return "Error";
                const composer = textarea.closest('form') || textarea.parentElement;
                if (!composer) return "Error";
                const buttons = Array.from(composer.querySelectorAll('button'));
                let selector_btn = null;
                for (const btn of buttons) {
                    const text = (btn.innerText || '').trim().toLowerCase();
                    if (text.includes("project")) continue;
                    const isSend = btn.getAttribute('data-testid') === 'send-button';
                    if (["instant", "thinking", "pro", "model", "auto-switch", "модель"].some(k => text.includes(k)) && !isSend) {
                        selector_btn = btn; break;
                    }
                }
                if (selector_btn) {
                    selector_btn.focus();
                    selector_btn.click();
                    return selector_btn.innerText;
                }
                return "Error";
            })()
        """)
        if btn_text == "Error":
            return
            
        await asyncio.sleep(1.5)
        model_selected = False
        
        # Вниз
        for i in range(8):
            await send_key(self.page, "ArrowDown", 40)
            await asyncio.sleep(0.3)
            active_text = await self.page.evaluate("document.activeElement ? document.activeElement.innerText : ''")
            if active_text and target_model.lower() in active_text.lower() and 'project' not in active_text.lower():
                await send_key(self.page, "Enter", 13)
                await asyncio.sleep(1.5)
                model_selected = True
                break
                
        if not model_selected:
            # Вверх
            for i in range(8):
                await send_key(self.page, "ArrowUp", 38)
                await asyncio.sleep(0.3)
                active_text = await self.page.evaluate("document.activeElement ? document.activeElement.innerText : ''")
                if active_text and target_model.lower() in active_text.lower() and 'project' not in active_text.lower():
                    await send_key(self.page, "Enter", 13)
                    await asyncio.sleep(1.5)
                    break

    async def _scroll_to_bottom(self):
        try:
            await self.page.evaluate("""
                (() => {
                    const scrollables = Array.from(document.querySelectorAll('*')).filter(e => {
                        const s = window.getComputedStyle(e);
                        return (s.overflowY === 'auto' || s.overflowY === 'scroll') && e.scrollHeight > e.clientHeight;
                    });
                    scrollables.forEach(c => c.scrollTo({ top: c.scrollHeight, behavior: 'smooth' }));
                    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
                })()
            """)
        except Exception:
            pass

    async def get_current_url(self) -> str:
        try:
            return await self.page.evaluate("location.href")
        except Exception:
            return ""

    async def _collect_files(self) -> str:
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
            canvas_code = await self.page.evaluate("""
                (() => {
                    const cm = document.querySelector('.cm-content');
                    if (!cm) return '';
                    return cm.innerText || '';
                })()
            """)
            if canvas_code and canvas_code.strip():
                return f"\n\n[Canvas]\n```python\n{canvas_code.strip()}\n```"
        except Exception:
            pass
        return ""

    async def send_prompt_and_stream(self, prompt_text: str, target_model: str, is_new_chat: bool):
        async with self._lock:
            if is_new_chat:
                logger.info("=== НАЧАЛО НОВОГО ЧАТА ===")
                await self.page.get(CHATGPT_URL)
                await asyncio.sleep(3.5)
                await self.select_model(target_model)
            else:
                logger.info("=== ПРОДОЛЖЕНИЕ ТЕКУЩЕГО ЧАТА ===")
            
            textarea = await find_element(self.page, "#prompt-textarea", timeout=10)
            if not textarea:
                yield "Error: prompt textarea not found"
                return
                
            logger.info("Ввод промпта (длина {} символов)...", len(prompt_text))
            await textarea.click()
            await asyncio.sleep(0.5)
            # Если текст огромный (это новый чат с историей), вводим быстро через JS, иначе - руками
            if len(prompt_text) > 2000 and is_new_chat:
                logger.info("Текст слишком длинный, вставляем через буфер/JS...")
                await self.page.evaluate(f'''
                    const ta = document.querySelector("#prompt-textarea");
                    ta.value = `{prompt_text.replace("`", "\\`")}`;
                    ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
                ''')
                await asyncio.sleep(1)
            else:
                await human_type(textarea, prompt_text)
                
            await asyncio.sleep(1.0)
            send_btn = await find_element(self.page, '[data-testid="send-button"]', timeout=3)
            if send_btn:
                await send_btn.click()
            else:
                await textarea.send_keys("\\n")
                
            logger.info("Запрос отправлен. Ждем генерацию...")
            
            elapsed = 0.0
            last_text = ""
            
            # Ожидание начала генерации
            while elapsed < 20:
                try:
                    stop_btn = await self.page.select('button[aria-label="Stop generating"], button[data-testid="stop-button"]', timeout=0.3)
                    if stop_btn:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
                elapsed += 0.5
                
            # Стриминг
            elapsed = 0.0
            while elapsed < GENERATION_TIMEOUT:
                if int(elapsed * 10) % 30 == 0:
                    await self._scroll_to_bottom()
                current_text = ""
                try:
                    current_text = await self.page.evaluate("""
                        (() => {
                            const msgs = document.querySelectorAll('div[data-message-author-role="assistant"]');
                            if (msgs.length === 0) return '';
                            return msgs[msgs.length - 1].innerText;
                        })()
                    """)
                except Exception:
                    pass
                
                if current_text and current_text != last_text:
                    new_chunk = current_text[len(last_text):]
                    last_text = current_text
                    yield new_chunk
                    
                stop_present = False
                try:
                    stop_el = await self.page.select('button[aria-label="Stop generating"], button[data-testid="stop-button"]', timeout=0.2)
                    if stop_el: stop_present = True
                except Exception:
                    pass
                    
                if not stop_present:
                    try:
                        send_el = await self.page.select('[data-testid="send-button"], button[data-testid="composer-send-button"]', timeout=0.2)
                    except Exception:
                        send_el = None

                    if send_el or elapsed > 5:
                        logger.info("Генерация завершена успешно.")
                        break

                await asyncio.sleep(0.3)
                elapsed += 0.3

            # Детектор файлов: клик по кнопкам скачивания в последнем сообщении
            await self._scroll_to_bottom()
            await asyncio.sleep(0.5)
            try:
                await self.page.evaluate("""
                    (() => {
                        const msgs = document.querySelectorAll('div[data-message-author-role="assistant"]');
                        if (!msgs.length) return;
                        const last = msgs[msgs.length - 1];
                        const clickables = last.querySelectorAll('a[download], button[aria-label*="ownload"], button[aria-label*="качать"]');
                        clickables.forEach(el => { try { el.click(); } catch(e){} });
                    })()
                """)
            except Exception:
                pass
            await asyncio.sleep(2.0)

            # Сборщик файлов: дописываем скачанные файлы / Canvas в ответ
            file_blob = await self._collect_files()
            if file_blob:
                yield file_blob
