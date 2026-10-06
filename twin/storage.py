"""Память двойника в SQLite: история чатов, разрешённые чаты, факты, исправления, вопросы к владельцу."""

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id   INTEGER PRIMARY KEY,
    title     TEXT NOT NULL,
    kind      TEXT NOT NULL,              -- private | group
    status    TEXT NOT NULL,              -- allowed | pending | blocked
    mode      TEXT NOT NULL DEFAULT 'all' -- all | mentions
);
CREATE TABLE IF NOT EXISTS messages (
    chat_id     INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    author      TEXT NOT NULL,            -- имя для модели
    role        TEXT NOT NULL,            -- twin | owner | other
    text        TEXT NOT NULL,
    reply_to    INTEGER,
    date        REAL NOT NULL,
    PRIMARY KEY (chat_id, msg_id)
);
CREATE TABLE IF NOT EXISTS facts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    text    TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS corrections (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    context TEXT NOT NULL,                -- переписка до ответа
    bad     TEXT NOT NULL,                -- что ответил двойник
    good    TEXT NOT NULL,                -- как ответили бы вы
    created REAL NOT NULL
);
-- Сообщения в личке владельца, на которые можно ответить: отчёты и вопросы.
CREATE TABLE IF NOT EXISTS owner_threads (
    owner_msg_id INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,           -- report | question | access
    chat_id      INTEGER NOT NULL,
    target_id    INTEGER,                 -- сообщение двойника (report) или собеседника (question)
    payload      TEXT NOT NULL DEFAULT ''
);
"""


@dataclass
class Chat:
    chat_id: int
    title: str
    kind: str
    status: str
    mode: str


@dataclass
class StoredMessage:
    msg_id: int
    author: str
    role: str
    text: str
    reply_to: int | None
    date: float


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    # ---------- чаты ----------

    def get_chat(self, chat_id: int) -> Chat | None:
        row = self.db.execute(
            "SELECT chat_id, title, kind, status, mode FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return Chat(*row) if row else None

    def upsert_chat(self, chat_id: int, title: str, kind: str, status: str | None = None, mode: str | None = None) -> Chat:
        current = self.get_chat(chat_id)
        status = status or (current.status if current else "pending")
        mode = mode or (current.mode if current else "all")
        self.db.execute(
            "INSERT INTO chats (chat_id, title, kind, status, mode) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, status = excluded.status, mode = excluded.mode",
            (chat_id, title, kind, status, mode),
        )
        self.db.commit()
        return Chat(chat_id, title, kind, status, mode)

    def list_chats(self) -> list[Chat]:
        rows = self.db.execute("SELECT chat_id, title, kind, status, mode FROM chats ORDER BY kind, title").fetchall()
        return [Chat(*r) for r in rows]

    # ---------- история ----------

    def add_message(self, chat_id: int, msg_id: int, author: str, role: str, text: str,
                    reply_to: int | None = None, date: float | None = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO messages (chat_id, msg_id, author, role, text, reply_to, date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, msg_id, author, role, text, reply_to, date or time.time()),
        )
        self.db.commit()

    def update_message_text(self, chat_id: int, msg_id: int, text: str) -> None:
        self.db.execute("UPDATE messages SET text = ? WHERE chat_id = ? AND msg_id = ?", (text, chat_id, msg_id))
        self.db.commit()

    def history(self, chat_id: int, limit: int, before_msg_id: int | None = None) -> list[StoredMessage]:
        query = "SELECT msg_id, author, role, text, reply_to, date FROM messages WHERE chat_id = ?"
        params: list = [chat_id]
        if before_msg_id is not None:
            query += " AND msg_id < ?"
            params.append(before_msg_id)
        query += " ORDER BY msg_id DESC LIMIT ?"
        params.append(limit)
        rows = self.db.execute(query, params).fetchall()
        return [StoredMessage(*r) for r in reversed(rows)]

    def last_owner_message_time(self, chat_id: int) -> float:
        row = self.db.execute(
            "SELECT MAX(date) FROM messages WHERE chat_id = ? AND role = 'owner'", (chat_id,)
        ).fetchone()
        return row[0] or 0.0

    # ---------- знания ----------

    def add_fact(self, text: str) -> int:
        cur = self.db.execute("INSERT INTO facts (text, created) VALUES (?, ?)", (text, time.time()))
        self.db.commit()
        return cur.lastrowid

    def facts(self) -> list[tuple[int, str]]:
        return self.db.execute("SELECT id, text FROM facts ORDER BY id").fetchall()

    def add_correction(self, context: str, bad: str, good: str) -> int:
        cur = self.db.execute(
            "INSERT INTO corrections (context, bad, good, created) VALUES (?, ?, ?, ?)",
            (context, bad, good, time.time()),
        )
        self.db.commit()
        return cur.lastrowid

    def corrections(self) -> list[tuple[int, str, str, str]]:
        return self.db.execute("SELECT id, context, bad, good FROM corrections ORDER BY id").fetchall()

    def forget(self, ref: str) -> bool:
        """ref: «12» — факт №12, «и12» — исправление №12."""
        ref = ref.strip().lower()
        table = "facts"
        if ref[:1] in ("и", "c", "i"):
            table, ref = "corrections", ref[1:]
        if not ref.isdigit():
            return False
        cur = self.db.execute(f"DELETE FROM {table} WHERE id = ?", (int(ref),))
        self.db.commit()
        return cur.rowcount > 0

    # ---------- переписка с владельцем ----------

    def add_owner_thread(self, owner_msg_id: int, kind: str, chat_id: int,
                         target_id: int | None = None, payload: str = "") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO owner_threads (owner_msg_id, kind, chat_id, target_id, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (owner_msg_id, kind, chat_id, target_id, payload),
        )
        self.db.commit()

    def get_owner_thread(self, owner_msg_id: int) -> tuple[str, int, int | None, str] | None:
        return self.db.execute(
            "SELECT kind, chat_id, target_id, payload FROM owner_threads WHERE owner_msg_id = ?",
            (owner_msg_id,),
        ).fetchone()
