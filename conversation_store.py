import sqlite3
import json
import hashlib
import time
import threading
from contextlib import closing
from typing import List, Optional, Tuple, Any

DB_PATH = r"W:\_python\APIPROXY\proxy_state.db"


def hash_message(role: str, content: Any) -> str:
    if isinstance(content, list):
        content = " ".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return hashlib.md5(f"{role}:{content}".encode("utf-8")).hexdigest()


class ConversationStore:
    """Персистентное сопоставление диалогов с URL чатов ChatGPT (SQLite)."""

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
                    chat_url TEXT,
                    hashes_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def _msg_hashes(self, messages) -> List[str]:
        out = []
        for m in messages:
            role = m.role if hasattr(m, "role") else m["role"]
            content = m.content if hasattr(m, "content") else m["content"]
            out.append(hash_message(role, content))
        return out

    def match(self, messages) -> Tuple[bool, list, Optional[str], Optional[int]]:
        """Возвращает (is_new_chat, delta_messages, chat_url, row_id).

        Ищет сохранённый диалог, чей список хэшей — префикс входящих сообщений.
        Берёт самый длинный (самый специфичный) матч.
        """
        incoming = self._msg_hashes(messages)
        best = None  # (row_id, stored_hashes, chat_url)
        with self._lock, closing(self._conn()) as conn, conn:
            rows = conn.execute(
                "SELECT id, chat_url, hashes_json FROM conversations"
            ).fetchall()
        for row_id, chat_url, hashes_json in rows:
            stored = json.loads(hashes_json)
            if len(stored) > len(incoming):
                continue
            if incoming[: len(stored)] == stored:
                if best is None or len(stored) > len(best[1]):
                    best = (row_id, stored, chat_url)
        if best is None:
            return True, list(messages), None, None
        row_id, stored, chat_url = best
        delta = list(messages)[len(stored):]
        return False, delta, chat_url, row_id

    def upsert(self, row_id: Optional[int], messages, assistant_reply: str,
               chat_url: Optional[str]) -> int:
        hashes = self._msg_hashes(messages)
        hashes.append(hash_message("assistant", assistant_reply))
        payload = json.dumps(hashes)
        now = time.time()
        with self._lock, closing(self._conn()) as conn, conn:
            if row_id is None:
                cur = conn.execute(
                    "INSERT INTO conversations (chat_url, hashes_json, updated_at) VALUES (?, ?, ?)",
                    (chat_url, payload, now),
                )
                return cur.lastrowid
            conn.execute(
                "UPDATE conversations SET chat_url=?, hashes_json=?, updated_at=? WHERE id=?",
                (chat_url, payload, now, row_id),
            )
            return row_id
