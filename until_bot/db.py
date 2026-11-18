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
    theme: str = "dark"


@dataclass
class Event:
    id: int
    chat_id: int
    title: str
    date: date
    created: date


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
                date    TEXT NOT NULL,
                created TEXT
            );
            CREATE INDEX IF NOT EXISTS events_chat ON events(chat_id);
            """
        )
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Добавляет новые колонки в базу, созданную старой версией бота."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)")}
        if "theme" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN theme TEXT NOT NULL DEFAULT 'dark'")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(events)")}
        if "created" not in cols:
            # Для старых дат точка отсчёта прогресса — день обновления бота.
            self.conn.execute("ALTER TABLE events ADD COLUMN created TEXT")
            self.conn.execute("UPDATE events SET created = date('now') WHERE created IS NULL")

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
        if field not in {"tz", "notify_time", "last_sent", "theme"}:
            raise ValueError(field)
        self.conn.execute(f"UPDATE users SET {field} = ? WHERE chat_id = ?", (value, chat_id))
        self.conn.commit()

    def delete_user(self, chat_id: int) -> None:
        self.conn.execute("DELETE FROM events WHERE chat_id = ?", (chat_id,))
        self.conn.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
        self.conn.commit()

    # --- события ---
    def add_event(self, chat_id: int, title: str, when: date, created: date) -> int:
        cur = self.conn.execute(
            "INSERT INTO events (chat_id, title, date, created) VALUES (?, ?, ?, ?)",
            (chat_id, title, when.isoformat(), created.isoformat()),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def events(self, chat_id: int) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE chat_id = ? ORDER BY date, id", (chat_id,)
        )
        return [self._event(r) for r in rows]

    def get_event(self, chat_id: int, event_id: int) -> Event | None:
        row = self.conn.execute(
            "SELECT * FROM events WHERE chat_id = ? AND id = ?", (chat_id, event_id)
        ).fetchone()
        return self._event(row) if row else None

    @staticmethod
    def _event(r: sqlite3.Row) -> Event:
        target = date.fromisoformat(r["date"])
        created = date.fromisoformat(r["created"]) if r["created"] else target
        return Event(r["id"], r["chat_id"], r["title"], target, created)

    def update_event(self, chat_id: int, event_id: int, *, title: str | None = None,
                     when: date | None = None, created: date | None = None) -> None:
        if title is not None:
            self.conn.execute("UPDATE events SET title = ? WHERE chat_id = ? AND id = ?",
                              (title, chat_id, event_id))
        if when is not None:
            self.conn.execute("UPDATE events SET date = ? WHERE chat_id = ? AND id = ?",
                              (when.isoformat(), chat_id, event_id))
        if created is not None:
            self.conn.execute("UPDATE events SET created = ? WHERE chat_id = ? AND id = ?",
                              (created.isoformat(), chat_id, event_id))
        self.conn.commit()

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
