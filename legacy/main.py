"""
ChatGPT Automation via nodriver.

Copies Chrome profile data (Local State + Profile 2) to a working directory,
launches Chrome with that profile, selects a specific model for each prompt,
sends a sequence of prompts, simulates human scrolling, streams response in real-time,
and cleanly extracts python code from <pre><code> elements.
"""

import asyncio
import random
import shutil
import sys
import os
import re
import warnings

# Suppress asyncio pipe warnings on Windows
warnings.filterwarnings('ignore', category=ResourceWarning)

from loguru import logger
import nodriver as uc

# ──────────────────────────── Configuration ────────────────────────────
ORIGINAL_CHROME_USER_DATA = os.path.expandvars(
    r"%LOCALAPPDATA%\Google\Chrome\User Data"
)
WORKING_PROFILE_DIR = r"W:\_python\APIPROXY\chrome_work_profile"
CHROME_PROFILE_NAME = "Profile 2"

# Промпты с указанием целевой модели
PROMPTS_CHAIN = [
    {
        "model": "Thinking",
        "text": "Напиши подробный план разработки классической игры Lode Runner на Python (используя pygame). Опиши основные классы (Игрок, Враги, Карта, Золото), структуру игрового цикла и физику. Код пока писать не нужно, только архитектуру."
    },
    {
        "model": "Thinking",
        "text": "Отлично. Теперь на основе этого плана напиши полный, рабочий код игры Lode Runner. Постарайся уместить логику в один файл. Напиши весь код."
    },
    {
        "model": "Thinking",
        "text": "Теперь проведи строгую самопроверку написанного кода. Выступи в роли Senior Python разработчика. Проверь код на наличие багов, проблем с коллизиями, залипанием в текстурах или утечек памяти. Исправь все найденные ошибки и выдай финальный, вылизанный блок кода игры. ВАЖНО: Выведи весь код игры в виде одного классического markdown-блока ```python ... ``` прямо в тексте ответа! Категорически запрещено использовать Canvas, артефакты и прикрепленные файлы!"
    }
]

RESULT_FILE = r"W:\_python\APIPROXY\result.txt"
CODE_FILE = r"W:\_python\APIPROXY\lode_runner.py"

CHATGPT_URL = "https://chatgpt.com"
PAGE_LOAD_TIMEOUT = 20        # seconds to wait for UI load
GENERATION_TIMEOUT = 240      # seconds max to wait for response (increased for coding)
TYPING_DELAY = (0.02, 0.07)   # min/max seconds per character


# ───────────────────────── Profile Management ──────────────────────────
def setup_profile() -> None:
    logger.info("Подготовка рабочего профиля Chrome...")
    if not os.path.exists(ORIGINAL_CHROME_USER_DATA):
        logger.warning(
            "Оригинальная папка Chrome User Data не найдена: {}",
            ORIGINAL_CHROME_USER_DATA,
        )
        return

    os.makedirs(WORKING_PROFILE_DIR, exist_ok=True)

    src_local_state = os.path.join(ORIGINAL_CHROME_USER_DATA, "Local State")
    dst_local_state = os.path.join(WORKING_PROFILE_DIR, "Local State")
    if os.path.exists(src_local_state):
        shutil.copy2(src_local_state, dst_local_state)
        logger.info("Local State скопирован.")

    src_profile = os.path.join(ORIGINAL_CHROME_USER_DATA, CHROME_PROFILE_NAME)
    dst_profile = os.path.join(WORKING_PROFILE_DIR, CHROME_PROFILE_NAME)
    if os.path.exists(src_profile):
        if not os.path.exists(dst_profile):
            logger.info("Копирование профиля {} ...", CHROME_PROFILE_NAME)
            shutil.copytree(src_profile, dst_profile, dirs_exist_ok=True)
            logger.info("Профиль {} скопирован.", CHROME_PROFILE_NAME)
        else:
            logger.info(
                "Рабочий профиль {} уже существует, копирование пропущено.",
                CHROME_PROFILE_NAME,
            )


# ─────────────────────── Helper: human typing ─────────────────────────
async def human_type(element, text: str) -> None:
    for char in text:
        await element.send_keys(char)
        await asyncio.sleep(random.uniform(*TYPING_DELAY))


# ──────────────────── Helper: find element with retry ─────────────────
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


# ───────────────────────── Model Selection ────────────────────────────
import nodriver.cdp.input_ as cdp_input

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

async def select_model(page, target_model: str) -> None:
    logger.info("Поиск и клик по кнопке выбора модели в композиторе...")
    
    btn_text = await page.evaluate("""
        (() => {
            const textarea = document.querySelector("#prompt-textarea");
            if (!textarea) return "Error: #prompt-textarea not found";
            
            const composer = textarea.closest('form') || textarea.parentElement;
            if (!composer) return "Error: parent composer container not found";
            
            const buttons = Array.from(composer.querySelectorAll('button'));
            let selector_btn = null;
            
            for (const btn of buttons) {
                const text = (btn.innerText || '').trim();
                const text_clean = text.toLowerCase();
                
                if (text_clean.includes("project")) {
                    continue;
                }
                
                // Исключаем только кнопку отправки по ее data-testid
                const isSend = btn.getAttribute('data-testid') === 'send-button';
                
                // Кнопка переключения моделей содержит "instant", "thinking", "pro", "model", "auto-switch"
                const isModelBtn = ["instant", "thinking", "pro", "model", "auto-switch", "модель"].some(k => text_clean.includes(k));
                
                if (isModelBtn && !isSend) {
                    selector_btn = btn;
                    break;
                }
            }
            
            if (!selector_btn) {
                for (const btn of buttons) {
                    const hasPopup = btn.getAttribute('aria-haspopup');
                    const isSend = btn.getAttribute('data-testid') === 'send-button';
                    const text = (btn.innerText || '').trim();
                    if (hasPopup && !isSend && text.length > 0) {
                        selector_btn = btn;
                        break;
                    }
                }
            }
            
            if (!selector_btn) {
                for (const btn of buttons) {
                    const text = (btn.innerText || '').trim();
                    if (["instant", "thinking", "pro"].some(k => text.toLowerCase().includes(k))) {
                        selector_btn = btn;
                        break;
                    }
                }
            }
            
            if (selector_btn) {
                // Radix UI dropdowns require pointerdown/mousedown before click
                selector_btn.focus();
                selector_btn.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
                selector_btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                selector_btn.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
                selector_btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                selector_btn.click();
                return selector_btn.innerText || "Dropdown clicked (no innerText)";
            }
            return "Error: model button not found in composer";
        })()
    """)

    if btn_text.startswith("Error:"):
        logger.error(btn_text)
        return

    logger.info("Кнопка выбора модели найдена в композиторе ('{}') и нажата. Ожидаем меню...", btn_text.strip().replace("\n", " "))
    try:
        await send_key(page, "Enter", 13)
    except Exception:
        pass
    await asyncio.sleep(random.uniform(1.2, 1.8))

    logger.info("Пытаемся выбрать модель '{}' с помощью аппаратно-эмулируемой клавиатуры...", target_model)
    model_selected = False
    
    # Сначала пробуем идти вниз по списку (код 40)
    for i in range(8):
        await send_key(page, "ArrowDown", 40)
        await asyncio.sleep(0.4)
        active_text = await page.evaluate("document.activeElement ? document.activeElement.innerText : ''")
        if active_text and target_model.lower() in active_text.lower() and 'project' not in active_text.lower():
            logger.info("Нашли модель '{}' (фокус на: '{}'), нажимаем Enter!", target_model, active_text.strip().replace('\n', ' '))
            await send_key(page, "Enter", 13)
            await asyncio.sleep(random.uniform(1.5, 2.5))
            model_selected = True
            break

    if not model_selected:
        logger.warning("Не нашли вниз, пробуем идти вверх...")
        for i in range(8):
            await send_key(page, "ArrowUp", 38)
            await asyncio.sleep(0.4)
            active_text = await page.evaluate("document.activeElement ? document.activeElement.innerText : ''")
            if active_text and target_model.lower() in active_text.lower() and 'project' not in active_text.lower():
                logger.info("Нашли модель '{}' (фокус на: '{}') при движении вверх, нажимаем Enter!", target_model, active_text.strip().replace('\n', ' '))
                await send_key(page, "Enter", 13)
                await asyncio.sleep(random.uniform(1.5, 2.5))
                model_selected = True
                break

    if not model_selected:
        logger.warning("Не удалось переключить модель через клавиатуру! Закрываем меню.")
        await send_key(page, "Escape", 27)
        await asyncio.sleep(1.0)

    # --- СТРОГАЯ ПРОВЕРКА ПЕРЕКЛЮЧЕНИЯ ---
    logger.info("Проверяем, переключилась ли модель...")
    await asyncio.sleep(1.0)
    final_btn_text = await page.evaluate("""
        (() => {
            const possible_selectors = [
                'div[class*="composer"] button[type="button"]',
                'button[class*="group"]',
                'button[class*="cursor-pointer"]'
            ];
            for (const selector of possible_selectors) {
                const btns = document.querySelectorAll(selector);
                for (const b of btns) {
                    const txt = (b.innerText || '').toLowerCase();
                    if (txt.includes('instant') || txt.includes('thinking') || txt.includes('pro')) {
                        return b.innerText;
                    }
                }
            }
            return null;
        })()
    """)

    if final_btn_text:
        if target_model.lower() not in final_btn_text.lower():
            logger.error("ОШИБКА: Модель НЕ переключилась! Текущая: '{}', Ожидалась: '{}'", final_btn_text.strip().replace('\n', ' '), target_model)
            raise RuntimeError(f"Не удалось переключить модель на {target_model}")
        else:
            logger.info("Проверка пройдена: модель '{}' активна.", target_model)
    else:
        logger.warning("Не удалось найти кнопку модели для финальной проверки.")


# ───────────────────────── Sidebar Mimicry ────────────────────────────
async def mimic_sidebar_interaction(page) -> None:
    """
    Имитирует действия реального человека: периодически открывает боковую панель,
    просматривает историю чатов (скроллит ее) и закрывает обратно.
    """
    logger.info("🤖 Имитация человека: Проверяем историю чатов...")
    try:
        # 1. Открываем боковую панель
        res_open = await page.evaluate("""
            (() => {
                const openBtn = document.querySelector('[data-testid="open-sidebar-button"]') || 
                                document.querySelector('[aria-label*="Open sidebar"]') || 
                                document.querySelector('[aria-label*="Toggle sidebar"]');
                if (openBtn) {
                    openBtn.click();
                    return "Открыта";
                }
                return "Кнопка не найдена или уже открыта";
            })()
        """)
        logger.info("Имитация человека: Боковая панель: {}", res_open)
        await asyncio.sleep(random.uniform(2.0, 3.5))

        # 2. Имитируем скроллинг истории чатов
        res_scroll = await page.evaluate("""
            (() => {
                const historyContainer = document.querySelector('nav') || 
                                         document.querySelector('[data-sidebar-item="true"]')?.closest('div') ||
                                         document.querySelector('div[class*="sidebar"]');
                if (historyContainer) {
                    historyContainer.scrollTo({
                        top: Math.random() * 200,
                        behavior: 'smooth'
                    });
                    return "Проскроллена";
                }
                return "Контейнер истории не найден";
            })()
        """)
        logger.info("Имитация человека: История чатов: {}", res_scroll)
        await asyncio.sleep(random.uniform(1.5, 3.0))

        # 3. Закрываем боковую панель обратно, чтобы освободить место
        res_close = await page.evaluate("""
            (() => {
                const closeBtn = document.querySelector('[data-testid="close-sidebar-button"]') || 
                                 document.querySelector('[aria-label*="Close sidebar"]') || 
                                 document.querySelector('[aria-label*="Toggle sidebar"]');
                if (closeBtn) {
                    closeBtn.click();
                    return "Закрыта";
                }
                return "Кнопка закрытия не найдена";
            })()
        """)
        logger.info("Имитация человека: Закрытие панели: {}", res_close)
        await asyncio.sleep(random.uniform(1.0, 2.0))
    except Exception as exc:
        logger.warning("Не удалось выполнить имитацию сайдбара: {}", exc)


# ──────────────────────────── Main logic ──────────────────────────────
async def main() -> None:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    setup_profile()

    logger.info("Запуск браузера...")
    config = uc.Config()
    config.user_data_dir = WORKING_PROFILE_DIR
    config.add_argument(f"--profile-directory={CHROME_PROFILE_NAME}")

    # Очищаем старые логи
    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)

    browser = None
    try:
        browser = await uc.start(config)

        logger.info("Переход на {}...", CHATGPT_URL)
        page = await browser.get(CHATGPT_URL)

        # Человеческая задержка
        pause_time = random.uniform(2.5, 4.5)
        logger.info("Человеческая задержка на загрузку: {:.1f} сек...", pause_time)
        await asyncio.sleep(pause_time)

        # Защита от разлогина
        try:
            login_btn = await page.select('[data-testid="login-button"]', timeout=3)
        except Exception:
            login_btn = None

        if login_btn:
            logger.critical(
                "Обнаружена кнопка входа! Сессия неактивна. "
                "Запустите login_chrome.bat и авторизуйтесь."
            )
            return

        logger.info("Сессия активна. Начинаем сессию промптов по плану.")

        # Имитируем человеческую проверку истории чатов в самом начале
        if random.random() < 0.8:
            await mimic_sidebar_interaction(page)

        last_response_text = ""

        for idx, prompt_item in enumerate(PROMPTS_CHAIN, 1):
            model_name = prompt_item["model"]
            prompt_text = prompt_item["text"]

            logger.info("=== ЭТАП {}/{} ===", idx, len(PROMPTS_CHAIN))

            # Шаг 1: Выбор модели
            await select_model(page, model_name)

            # Шаг 2: Ввод промпта
            textarea = await find_element(page, "#prompt-textarea", timeout=15)
            if not textarea:
                logger.error("Поле ввода (#prompt-textarea) не найдено!")
                return

            logger.info("Ввод текста...")
            await textarea.click()
            await asyncio.sleep(random.uniform(0.4, 0.9))
            await human_type(textarea, prompt_text)

            # Пауза перед кликом
            await asyncio.sleep(random.uniform(0.6, 1.4))

            send_btn = None
            try:
                send_btn = await page.select('[data-testid="send-button"]', timeout=3)
            except Exception:
                pass

            if send_btn:
                await send_btn.click()
            else:
                await textarea.send_keys("\n")
            
            logger.info("Запрос {} отправлен. Ожидаем генерацию...", idx)

            # Шаг 3: Ожидание начала генерации
            elapsed = 0.0
            generation_started = False
            while elapsed < 30:
                try:
                    stop_btn = await page.select(
                        'button[aria-label="Stop generating"], button[data-testid="stop-button"]',
                        timeout=0.3
                    )
                    if stop_btn:
                        generation_started = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)
                elapsed += 0.3

            # Шаг 4: Поллинг с эмуляцией скроллинга и стриминга ответов
            elapsed = 0.0
            last_text = ""
            
            # Начинаем красивый стриминг в stdout
            sys.stdout.write(f"\n--- СТРИМИНГ ОТВЕТА {idx} ---\n")
            sys.stdout.flush()

            while elapsed < GENERATION_TIMEOUT:
                # ── НАСТОЯЩИЙ СКРОЛЛИНГ ──
                # Раз в ~3 секунды мягко скроллим контейнер вниз
                if int(elapsed * 10) % 30 == 0:
                    try:
                        await page.evaluate("""
                            (() => {
                                // 1. Находим все скроллируемые контейнеры сообщений
                                const scrollableElements = Array.from(document.querySelectorAll('*')).filter(e => {
                                    const s = window.getComputedStyle(e);
                                    return (s.overflowY === 'auto' || s.overflowY === 'scroll') && e.scrollHeight > e.clientHeight;
                                });
                                
                                scrollableElements.forEach(container => {
                                    container.scrollTo({
                                        top: container.scrollHeight,
                                        behavior: 'smooth'
                                    });
                                });
                                
                                // 2. Дополнительно прокручиваем всё окно браузера
                                window.scrollTo({
                                    top: document.body.scrollHeight,
                                    behavior: 'smooth'
                                });
                            })()
                        """)
                    except Exception:
                        pass

                # Стриминг: получаем текущий innerText
                current_text = ""
                try:
                    current_text = await page.evaluate("""
                        (() => {
                            const msgs = document.querySelectorAll('div[data-message-author-role="assistant"]');
                            if (msgs.length === 0) return '';
                            return msgs[msgs.length - 1].innerText;
                        })()
                    """)
                except Exception:
                    pass

                if current_text and current_text != last_text:
                    # Печатаем только добавленный фрагмент
                    new_chunk = current_text[len(last_text):]
                    sys.stdout.write(new_chunk)
                    sys.stdout.flush()
                    last_text = current_text

                # Проверяем кнопку Stop
                stop_present = False
                try:
                    stop_el = await page.select(
                        'button[aria-label="Stop generating"], button[data-testid="stop-button"]',
                        timeout=0.2
                    )
                    if stop_el:
                        stop_present = True
                except Exception:
                    pass

                if not stop_present:
                    try:
                        send_el = await page.select(
                            '[data-testid="send-button"], button[data-testid="composer-send-button"]',
                            timeout=0.2
                        )
                    except Exception:
                        send_el = None

                    if send_el or elapsed > 5:
                        logger.info("\nГенерация {} завершена. Итого: {:.1f}s", idx, elapsed)
                        break

                await asyncio.sleep(0.3)
                elapsed += 0.3
            else:
                raise TimeoutError(f"Таймаут генерации ответа на этапе {idx}")

            # Запоминаем последний текст
            last_response_text = last_text

            # Сохраняем логи общения
            with open(RESULT_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n\n{'='*20} ЭТАП {idx} {'='*20}\n")
                f.write(f"PROMPT: {prompt_text}\n\n")
                f.write(last_response_text)

            logger.info("Диалог этапа {} записан.", idx)

            # Мимикрия: чтение ответа и случайная проверка истории чатов перед следующим шагом
            if idx < len(PROMPTS_CHAIN):
                reading_pause = random.uniform(2.0, 4.0)
                logger.info("Имитируем чтение: пауза {:.1f} сек...", reading_pause)
                await asyncio.sleep(reading_pause)
                
                # С 70% вероятностью открываем сайдбар для имитации проверки старых чатов
                if random.random() < 0.7:
                    await mimic_sidebar_interaction(page)
                    
                # Дополнительная человеческая пауза после сайдбара
                await asyncio.sleep(random.uniform(2.0, 4.0))

        # ──────────────────── Парсинг финального кода ────────────────────
        logger.info("Извлекаем Python-код из блоков <pre><code>...")
        
        # Получаем чистый текст из пре форматированных блоков внутри последнего ответа
        final_code = await page.evaluate("""
            (() => {
                const msgs = document.querySelectorAll('div[data-message-author-role="assistant"]');
                if (msgs.length === 0) return '';
                const lastMsg = msgs[msgs.length - 1];
                const codeElements = lastMsg.querySelectorAll('pre code');
                if (codeElements.length === 0) return '';
                
                // Находим самый длинный блок (это и есть код игры)
                let longestCode = '';
                codeElements.forEach(el => {
                    let codeText = el.innerText || el.textContent;
                    if (codeText.length > longestCode.length) {
                        longestCode = codeText;
                    }
                });
                return longestCode;
            })()
        """)

        if final_code and final_code.strip():
            with open(CODE_FILE, "w", encoding="utf-8") as f:
                f.write(final_code.strip())
            logger.info("Финальный Python-код сохранен в: {}", CODE_FILE)
        else:
            logger.warning(
                "Теги pre code не найдены на странице! Пробуем регулярку по тексту..."
            )
            python_blocks = re.findall(
                r"```python\s*(.*?)\s*```", last_response_text, re.DOTALL
            )
            if python_blocks:
                final_code = max(python_blocks, key=len)
                with open(CODE_FILE, "w", encoding="utf-8") as f:
                    f.write(final_code.strip())
                logger.info("Финальный Python-код извлечен по регулярке в {}", CODE_FILE)
            else:
                logger.error("Не удалось найти код в последнем сообщении.")

        await asyncio.sleep(3)

    except TimeoutError as exc:
        logger.error("Таймаут: {}", exc)
    except Exception as exc:
        logger.exception("Ошибка: {}", exc)
    finally:
        if browser:
            logger.info("Закрытие браузера...")
            try:
                browser.stop()
            except Exception as exc:
                logger.error("Ошибка закрытия: {}", exc)


if __name__ == "__main__":
    asyncio.run(main())
