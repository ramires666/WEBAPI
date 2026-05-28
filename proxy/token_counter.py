"""Приблизительный подсчёт токенов для чата ChatGPT.

Метод: len(text) / 4 для латиницы, / 2 для кириллицы/CJK.
Точность: ±15-20%, достаточно для порога в 200k.
Не требует tiktoken / внешних зависимостей.
"""

import re
from typing import Any, List

_CYR_CJK = re.compile(r'[\u0400-\u04FF\u4E00-\u9FFF\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7AF]')


def estimate_tokens(text: str) -> int:
    """Оценка кол-ва токенов в тексте.

    Алгоритм:
    - Кириллица/CJK символы ≈ 1 токен на 2 символа
    - Латиница/ASCII ≈ 1 токен на 4 символа
    - Overhead на спец-символы/пунктуацию включён в оценку
    """
    if not text:
        return 0
    cyr_cjk_count = len(_CYR_CJK.findall(text))
    latin_count = len(text) - cyr_cjk_count
    return int(latin_count / 4 + cyr_cjk_count / 2)


def estimate_messages_tokens(messages: list) -> int:
    """Оценка суммарных токенов по всем сообщениям.

    Принимает list[ChatMessage] (pydantic) или list[dict].
    Учитывает overhead на роли и разделители (~4 токена/сообщение).
    """
    total = 0
    for m in messages:
        content = m.content if hasattr(m, "content") else m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        total += estimate_tokens(str(content))
        total += 4  # overhead: role, name, separators
    return total
