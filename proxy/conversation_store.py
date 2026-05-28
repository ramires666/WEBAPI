import sqlite3
import json
import hashlib
import re
import time
import threading
from contextlib import closing
from typing import List, Optional, Tuple, Any

DB_PATH = r"W:\_python\APIPROXY\proxy_state.db"


def _text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return content or ""


def hash_message(role: str, content: Any) -> str:
    return hashlib.md5(f"{role}:{_text(content)}".encode("utf-8")).hexdigest()


# Маркеры известных IDE (устойчивые подстроки названия продукта в system-промпте).
# Список расширяется по мере появления новых клиентов.
_CLIENT_MARKERS = [
    ("kilocode", ("kilo code", "kilocode", "you are kilo")),
    ("opencode", ("opencode",)),
    ("cline", ("cline",)),
    ("roo", ("roo code", "roo cline", "roocode")),
    ("cursor", ("cursor",)),
    ("continue", ("continue.dev",)),
    ("aider", ("aider",)),
]


def client_fingerprint(messages) -> str:
    """Идентификатор IDE по устойчивому маркеру названия продукта в system.
    Маркер не волатилен -> client_id стабилен -> продолжение чата не ломается.
    Неизвестный клиент -> 'generic' (тоже стабилен)."""
    sys_txt = ""
    for m in messages:
        role = m.role if hasattr(m, "role") else m["role"]
        if role == "system":
            sys_txt += _text(m.content if hasattr(m, "content") else m["content"]) + "\n"
    if not sys_txt.strip():
        return "default"
    low = sys_txt.lower()
    for cid, marks in _CLIENT_MARKERS:
        if any(mk in low for mk in marks):
            return cid
    return "generic"


class ConversationStore:
    """Сопоставление диалогов с URL чатов ChatGPT (SQLite), namespace по IDE."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._lock, closing(self._conn()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id TEXT NOT NULL,
                    chat_url TEXT,
                    hashes_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    token_count INTEGER DEFAULT 0,
                    summarized INTEGER DEFAULT 0,
                    browser_id TEXT
                )
                """
            )
            # Миграция: добавляем новые колонки к существующей БД
            for col, default in [("token_count", "0"), ("summarized", "0")]:
                try:
                    conn.execute(f"ALTER TABLE conversations ADD COLUMN {col} INTEGER DEFAULT {default}")
                except sqlite3.OperationalError:
                    pass  # колонка уже существует
            # Миграция: добавляем browser_id (TEXT)
            try:
                conn.execute("ALTER TABLE conversations ADD COLUMN browser_id TEXT")
            except sqlite3.OperationalError:
                pass  # колонка уже существует

    def _sig_hashes(self, messages) -> List[str]:
        """Подпись диалога: только USER-сообщения. Их клиент шлёт дословно при
        продолжении; ответы ассистента клиенты нормализуют/обрезают -> ненадёжны."""
        out = []
        for m in messages:
            role = m.role if hasattr(m, "role") else m["role"]
            if role != "user":
                continue
            
            # Извлекаем текст
            content = m.content if hasattr(m, "content") else m["content"]
            text = _text(content)
            
            # Kilo Code добавляет <environment_details> к последнему сообщению,
            # но удаляет его из старых сообщений. Нужно вырезать этот блок перед хешированием.
            text = re.sub(r'<environment_details>.*?</environment_details>', '', text, flags=re.DOTALL)
            
            out.append(hash_message(role, text.strip()))
        return out

    def match(self, messages) -> Tuple[bool, list, Optional[str], Optional[int], Optional[str]]:
        """(is_new_chat, delta_messages, chat_url, row_id, browser_id).

        Беседа ищется ТОЛЬКО внутри своей IDE (client_id из system) по префиксу
        подписи user/assistant — «привет» из разных IDE не схлопывается."""
        cid = client_fingerprint(messages)
        incoming = self._sig_hashes(messages)
        best = None
        with self._lock, closing(self._conn()) as conn, conn:
            rows = conn.execute(
                "SELECT id, chat_url, hashes_json, browser_id FROM conversations WHERE client_id = ?",
                (cid,),
            ).fetchall()
        for row_id, chat_url, hashes_json, browser_id in rows:
            stored = json.loads(hashes_json)
            if not stored or len(stored) > len(incoming):
                continue
            if incoming[: len(stored)] == stored:
                if best is None or len(stored) > len(best[1]):
                    best = (row_id, stored, chat_url, browser_id)
        if best is None:
            return True, list(messages), None, None, None
        row_id, stored, chat_url, browser_id = best
        # delta = непрожёванный хвост: всё ПОСЛЕ последнего ответа ассистента
        # (tool-результаты / новый user). Так продвигается агентский цикл Kilo,
        # а не перечитывается один и тот же экран (иначе бесконечный цикл).
        last_asst = -1
        for idx, m in enumerate(messages):
            role = m.role if hasattr(m, "role") else m["role"]
            if role == "assistant":
                last_asst = idx
        delta = list(messages[last_asst + 1:])
        return False, delta, chat_url, row_id, browser_id

    def upsert(self, row_id: Optional[int], messages, assistant_reply: str,
               chat_url: Optional[str], token_count: int = 0, browser_id: Optional[str] = None) -> int:
        cid = client_fingerprint(messages)
        hashes = self._sig_hashes(messages)
        payload = json.dumps(hashes)
        now = time.time()
        with self._lock, closing(self._conn()) as conn, conn:
            if row_id is None:
                cur = conn.execute(
                    "INSERT INTO conversations (client_id, chat_url, hashes_json, updated_at, token_count, browser_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (cid, chat_url, payload, now, token_count, browser_id),
                )
                return cur.lastrowid
            update_query = "UPDATE conversations SET chat_url=?, hashes_json=?, updated_at=?, token_count=?"
            params = [chat_url, payload, now, token_count]
            if browser_id:
                update_query += ", browser_id=?"
                params.append(browser_id)
            update_query += " WHERE id=?"
            params.append(row_id)
            conn.execute(update_query, params)
            return row_id
