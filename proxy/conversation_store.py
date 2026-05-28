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
                    updated_at REAL NOT NULL
                )
                """
            )

    def _sig_hashes(self, messages) -> List[str]:
        """Подпись диалога: только user/assistant (system/tools волатильны)."""
        out = []
        for m in messages:
            role = m.role if hasattr(m, "role") else m["role"]
            if role not in ("user", "assistant"):
                continue
            out.append(hash_message(role, m.content if hasattr(m, "content") else m["content"]))
        return out

    def match(self, messages) -> Tuple[bool, list, Optional[str], Optional[int]]:
        """(is_new_chat, delta_messages, chat_url, row_id).

        Беседа ищется ТОЛЬКО внутри своей IDE (client_id из system) по префиксу
        подписи user/assistant — «привет» из разных IDE не схлопывается."""
        cid = client_fingerprint(messages)
        incoming = self._sig_hashes(messages)
        best = None
        with self._lock, closing(self._conn()) as conn, conn:
            rows = conn.execute(
                "SELECT id, chat_url, hashes_json FROM conversations WHERE client_id = ?",
                (cid,),
            ).fetchall()
        for row_id, chat_url, hashes_json in rows:
            stored = json.loads(hashes_json)
            if not stored or len(stored) > len(incoming):
                continue
            if incoming[: len(stored)] == stored:
                if best is None or len(stored) > len(best[1]):
                    best = (row_id, stored, chat_url)
        if best is None:
            return True, list(messages), None, None
        row_id, stored, chat_url = best
        ua = [m for m in messages
              if (m.role if hasattr(m, "role") else m["role"]) in ("user", "assistant")]
        delta = ua[len(stored):]
        return False, delta, chat_url, row_id

    def upsert(self, row_id: Optional[int], messages, assistant_reply: str,
               chat_url: Optional[str]) -> int:
        cid = client_fingerprint(messages)
        hashes = self._sig_hashes(messages)
        hashes.append(hash_message("assistant", assistant_reply))
        payload = json.dumps(hashes)
        now = time.time()
        with self._lock, closing(self._conn()) as conn, conn:
            if row_id is None:
                cur = conn.execute(
                    "INSERT INTO conversations (client_id, chat_url, hashes_json, updated_at) VALUES (?, ?, ?, ?)",
                    (cid, chat_url, payload, now),
                )
                return cur.lastrowid
            conn.execute(
                "UPDATE conversations SET chat_url=?, hashes_json=?, updated_at=? WHERE id=?",
                (chat_url, payload, now, row_id),
            )
            return row_id
