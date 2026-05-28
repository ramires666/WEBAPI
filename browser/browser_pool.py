"""Пул из N браузеров для распределения нагрузки по нескольким ChatGPT-профилям.

- Round-robin при создании нового чата (is_new_chat=True).
- Sticky-биндинг при продолжении (chat_id → browser_id из conversation_store).
- При неудачном старте профиля (не залогинен/нет файлов) — помечается unavailable,
  пропускается в round-robin, прокси продолжает работу на оставшихся.
"""
import asyncio
from typing import Optional
from loguru import logger
from browser.browser_manager import BrowserManager


class BrowserPool:
    def __init__(self, profiles: list[str], profiles_dir: str, work_dir: str):
        self.managers: dict[str, BrowserManager] = {}
        self.unavailable: set[str] = set()
        self._idx = 0
        for name in profiles:
            self.managers[name] = BrowserManager(name, profiles_dir, work_dir)

    async def start_all(self):
        """Запуск всех браузеров параллельно. Упавшие → unavailable, не валим pool."""
        async def _start_one(name: str, mgr: BrowserManager):
            try:
                await mgr.start_browser()
                logger.info("[pool] {}: запущен", name)
            except Exception as e:
                self.unavailable.add(name)
                logger.error("[pool] {}: НЕ удалось запустить ({}) — помечен unavailable", name, e)
        await asyncio.gather(*[_start_one(n, m) for n, m in self.managers.items()])
        active = [n for n in self.managers if n not in self.unavailable]
        if not active:
            raise RuntimeError("[pool] Ни один браузер не запустился!")
        logger.info("[pool] Активные браузеры: {}", active)

    async def stop_all(self):
        for name, mgr in self.managers.items():
            try:
                await mgr.stop_browser()
            except Exception as e:
                logger.warning("[pool] {}: ошибка остановки ({})", name, e)

    def get(self, browser_id: Optional[str]) -> Optional[BrowserManager]:
        """Sticky lookup. None если browser_id не существует или unavailable."""
        if not browser_id:
            return None
        if browser_id in self.unavailable:
            return None
        return self.managers.get(browser_id)

    def next_for_new_chat(self) -> tuple[str, BrowserManager]:
        """Round-robin по активным браузерам. Бросает RuntimeError если все unavailable."""
        names = [n for n in self.managers if n not in self.unavailable]
        if not names:
            raise RuntimeError("[pool] Нет доступных браузеров для нового чата")
        name = names[self._idx % len(names)]
        self._idx = (self._idx + 1) % len(names)
        logger.info("[pool] Новый чат → {}", name)
        return name, self.managers[name]
