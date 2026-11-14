"""Хранилище на SQLite (стандартная библиотека, без внешних зависимостей)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date


@dataclass
class User:
    chat_id: int
    tz: str
    notify_time: str
    last_sent: str | None


@dataclass
class Event:
    id: int
    chat_id: int
    title: str
    date: date


class Storage:
    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id     INTEGER PRIMARY KEY,
                tz          TEXT NOT NULL,
                notify_time TEXT NOT NULL,
                last_sent   TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL REFERENCES users(chat_id),
                title   TEXT NOT NULL,
                date    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_chat ON events(chat_id);
            """
        )
        self.conn.commit()

    # --- пользователи ---
    def ensure_user(self, chat_id: int, tz: str, notify_time: str) -> User:
        self.conn.execute(
            "INSERT OR IGNORE INTO users (chat_id, tz, notify_time) VALUES (?, ?, ?)",
            (chat_id, tz, notify_time),
        )
        self.conn.commit()
        return self.get_user(chat_id)  # type: ignore[return-value]

    def get_user(self, chat_id: int) -> User | None:
        row = self.conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
        return User(**row) if row else None

    def all_users(self) -> list[User]:
        return [User(**r) for r in self.conn.execute("SELECT * FROM users")]

    def set_user_field(self, chat_id: int, field: str, value: str | None) -> None:
        if field not in {"tz", "notify_time", "last_sent"}:
            raise ValueError(field)
        self.conn.execute(f"UPDATE users SET {field} = ? WHERE chat_id = ?", (value, chat_id))
        self.conn.commit()

    def delete_user(self, chat_id: int) -> None:
        self.conn.execute("DELETE FROM events WHERE chat_id = ?", (chat_id,))
        self.conn.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
        self.conn.commit()

    # --- события ---
    def add_event(self, chat_id: int, title: str, when: date) -> int:
        cur = self.conn.execute(
            "INSERT INTO events (chat_id, title, date) VALUES (?, ?, ?)",
            (chat_id, title, when.isoformat()),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def events(self, chat_id: int) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE chat_id = ? ORDER BY date, id", (chat_id,)
        )
        return [
            Event(r["id"], r["chat_id"], r["title"], date.fromisoformat(r["date"])) for r in rows
        ]

    def delete_event(self, chat_id: int, event_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM events WHERE chat_id = ? AND id = ?", (chat_id, event_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def delete_past(self, chat_id: int, today: date) -> int:
        cur = self.conn.execute(
            "DELETE FROM events WHERE chat_id = ? AND date < ?", (chat_id, today.isoformat())
        )
        self.conn.commit()
        return cur.rowcount
