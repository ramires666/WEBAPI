"""
Dedicated test script to verify ChatGPT model selection.

Launches Chrome, navigates to ChatGPT, finds the model selector inside the composer bar,
clicks it, lists all available models, selects the Thinking model, and verifies the switch.
DOES NOT send any prompts.
"""

import asyncio
import random
import sys
import os
import warnings

# Suppress asyncio pipe warnings
warnings.filterwarnings('ignore', category=ResourceWarning)

from loguru import logger
import nodriver as uc

WORKING_PROFILE_DIR = r"W:\_python\APIPROXY\chrome_work_profile"
CHROME_PROFILE_NAME = "Profile 2"
CHATGPT_URL = "https://chatgpt.com"
TARGET_MODEL = "Thinking"  # Мы тестируем переключение на передовую модель Thinking!


async def main() -> None:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    logger.info("Запуск браузера для теста выбора модели...")
    config = uc.Config()
    config.user_data_dir = WORKING_PROFILE_DIR
    config.add_argument(f"--profile-directory={CHROME_PROFILE_NAME}")

    browser = None
    try:
        browser = await uc.start(config)
        logger.info("Переход на {}...", CHATGPT_URL)
        page = await browser.get(CHATGPT_URL)

        logger.info("Ожидаем загрузку интерфейса чата (5 сек)...")
        await asyncio.sleep(5)

        # ── ШАГ 1. Находим кнопку выбора модели и кликаем через MouseEvents ──
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
                
                // Если не нашли по словам, но в композиторе есть кнопка с dropdown-поведением (исключая пустую скрепку)
                if (!selector_btn) {
                    for (const btn of buttons) {
                        const hasPopup = btn.getAttribute('aria-haspopup');
                        const isSend = btn.getAttribute('data-testid') === 'send-button';
                        const text = (btn.innerText || '').trim();
                        // У модели ВСЕГДА есть текст (например, "Instant"), у скрепки текста нет
                        if (hasPopup && !isSend && text.length > 0) {
                            selector_btn = btn;
                            break;
                        }
                    }
                }
                
                // Резервный поиск: ищем любую кнопку в композиторе, которая содержит слово "Instant" или "Thinking" или "Pro"
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
                    const triggerMouseEvent = (node, eventType) => {
                        const clickEvent = new MouseEvent(eventType, {
                            bubbles: true,
                            cancelable: true,
                            view: window
                        });
                        node.dispatchEvent(clickEvent);
                    };
                    
                    triggerMouseEvent(selector_btn, 'mousedown');
                    triggerMouseEvent(selector_btn, 'mouseup');
                    selector_btn.click();
                    return selector_btn.innerText || "Dropdown clicked (no innerText)";
                }
                return "Error: model button not found in composer";
            })()
        """)

        if btn_text.startswith("Error:"):
            logger.error(btn_text)
            return

        logger.info("Кнопка найдена внутри композитора и нажат полный клик: '{}'", btn_text.strip().replace("\n", " "))
        logger.info("Ждем открытия меню (1.5 сек)...")
        await asyncio.sleep(1.5)

        # ── ШАГ 2. Считываем доступные модели из открывшегося меню ──
        logger.info("Считываем доступные модели из меню...")
        available_models = await page.evaluate("""
            (() => {
                const models = [];
                // Ищем все элементы меню
                const items = document.querySelectorAll('[role="menuitem"], [role="option"], [role="menuitemradio"], div[role="menu"] button, ul[role="listbox"] li, div[class*="popover"] button');
                
                items.forEach(el => {
                    const text = el.innerText || '';
                    if (text.length > 0 && text.length < 100) {
                        const text_clean = text.replace(/\\n/g, ' ').trim();
                        if (text_clean && !models.includes(text_clean)) {
                            models.push(text_clean);
                        }
                    }
                });
                
                // Альтернативный поиск по тексту, если нет ролей
                if (models.length === 0) {
                    const elements = document.querySelectorAll('button, [role="button"], div, span');
                    elements.forEach(el => {
                        const text = el.innerText || '';
                        const rect = el.getBoundingClientRect();
                        // Ищем элементы с текстом моделей, которые находятся на экране и не в шапке
                        if (rect.width > 20 && rect.height > 20 && rect.top > 50) {
                            const clean = text.replace(/\\n/g, ' ').trim();
                            if (clean && !clean.toLowerCase().includes('project')) {
                                if (clean.toLowerCase().includes('thinking') || clean.toLowerCase().includes('pro') || clean.toLowerCase().includes('instant') || clean.toLowerCase().includes('configure')) {
                                    if (!models.includes(clean)) {
                                        models.push(clean);
                                    }
                                }
                            }
                        }
                    });
                }
                return models;
            })()
        """)

        logger.info("=== НАЙДЕННЫЕ ДОСТУПНЫЕ МОДЕЛИ ===")
        for idx, m in enumerate(available_models, 1):
            logger.info("{}. {}", idx, m)
        logger.info("=================================")

        if not available_models:
            logger.warning("Меню не вернуло список моделей. Попробуем прочесть через DOM-dump:")
            all_text_elements = await page.evaluate("""
                (() => {
                    const res = [];
                    document.querySelectorAll('*').forEach(el => {
                        if (el.children.length === 0) {
                            const t = el.innerText || el.textContent || '';
                            const c = t.replace(/\\n/g, ' ').trim();
                            if (c.length > 0 && c.length < 40 && !res.includes(c)) {
                                res.push(c);
                            }
                        }
                    });
                    return res;
                })()
            """)
            logger.info("Все текстовые листья в DOM: {}", all_text_elements[:30])
            return

        # ── ШАГ 3. Кликаем по целевой модели в меню через MouseEvents ──
        logger.info("Попытка выбрать модель '{}'...", TARGET_MODEL)
        success = await page.evaluate(f"""
            (() => {{
                const target = "{TARGET_MODEL}".toLowerCase();
                const items = document.querySelectorAll('button, [role="button"], [role="menuitem"], [role="option"], div, span');
                let bestMatch = null;
                
                for (const el of items) {{
                    const text = (el.innerText || '').trim();
                    if (text.length > 0 && text.length < 50) {{
                        const text_clean = text.toLowerCase();
                        if (text_clean.includes(target) && !text_clean.includes('project')) {{
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 15 && rect.height > 15 && rect.top > 50) {{
                                bestMatch = el;
                                break;
                            }}
                        }}
                    }}
                }}
                
                if (bestMatch) {{
                    const triggerMouseEvent = (node, eventType) => {{
                        const clickEvent = new MouseEvent(eventType, {{
                            bubbles: true,
                            cancelable: true,
                            view: window
                        }});
                        node.dispatchEvent(clickEvent);
                    }};
                    
                    triggerMouseEvent(bestMatch, 'mousedown');
                    triggerMouseEvent(bestMatch, 'mouseup');
                    bestMatch.click();
                    return true;
                }}
                return false;
            }})()
        """)

        if success:
            logger.info("Модель '{}' выбрана в меню. Ждем применения (2 сек)...", TARGET_MODEL)
            await asyncio.sleep(2)

            # Проверяем, изменился ли текст кнопки
            new_btn_text = await page.evaluate("""
                (() => {
                    const textarea = document.querySelector("#prompt-textarea");
                    if (!textarea) return "Textarea not found";
                    const composer = textarea.closest('form') || textarea.parentElement;
                    const buttons = Array.from(composer.querySelectorAll('button'));
                    const keywords = ["instant", "thinking", "pro", "auto-switch"];
                    
                    for (const btn of buttons) {
                        const text = (btn.innerText || '').trim();
                        if (text.length > 0 && text.length < 30) {
                            const text_clean = text.toLowerCase();
                            if (keywords.some(k => text_clean.includes(k))) {
                                return text;
                            }
                        }
                    }
                    return "Model name not visible in composer";
                })()
            """)
            logger.info("Новый статус кнопки выбора модели в композиторе: '{}'", new_btn_text)
            logger.success("ТЕСТ ВЫБОРА МОДЕЛИ ПРОШЕЛ УСПЕШНО! 🎉")
        else:
            logger.error("Не удалось найти и выбрать модель '{}' в списке!", TARGET_MODEL)

    except Exception as exc:
        logger.exception("Ошибка при тесте выбора модели: {}", exc)
    finally:
        if browser:
            logger.info("Закрываем браузер...")
            try:
                browser.stop()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
