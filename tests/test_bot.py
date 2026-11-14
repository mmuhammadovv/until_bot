import asyncio
from datetime import date, timedelta

import pytest

from until_bot import bot as botmod
from until_bot.db import Storage
from until_bot.utils import describe, local_now, parse_date, parse_time, parse_tz, plural_days


@pytest.mark.parametrize(
    "n,word",
    [(1, "день"), (2, "дня"), (4, "дня"), (5, "дней"), (11, "дней"), (12, "дней"),
     (21, "день"), (22, "дня"), (100, "дней"), (101, "день"), (111, "дней")],
)
def test_plural(n, word):
    assert plural_days(n) == word


def test_parse_date():
    today = date(2026, 11, 14)
    assert parse_date("31.12.2026", today) == date(2026, 12, 31)
    assert parse_date("1.1.27", today) == date(2027, 1, 1)
    assert parse_date("2027-03-08", today) == date(2027, 3, 8)
    assert parse_date("15.06", today) == date(2027, 6, 15)  # уже прошло в этом году
    assert parse_date("20.11", today) == date(2026, 11, 20)
    for bad in ["32.01.2026", "abc", "1", "29.02.2027"]:
        with pytest.raises(ValueError):
            parse_date(bad, today)


def test_parse_tz_and_time():
    assert parse_tz("+3").utcoffset(None) == timedelta(hours=3)
    assert parse_tz("UTC-5:30").utcoffset(None) == -timedelta(hours=5, minutes=30)
    assert str(parse_tz("Asia/Tashkent")) == "Asia/Tashkent"
    with pytest.raises(ValueError):
        parse_tz("Mars/Base")
    assert parse_time("8:05").strftime("%H:%M") == "08:05"
    with pytest.raises(ValueError):
        parse_time("25:00")


def test_describe():
    t = date(2026, 11, 14)
    assert "<b>47</b> дней" in describe("НГ", date(2026, 12, 31), t)
    assert "сегодня" in describe("НГ", t, t)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def test_send_daily_once_per_day(tmp_path, monkeypatch):
    monkeypatch.setattr(botmod, "SEND_DELAY", 0)
    botmod.storage = Storage(str(tmp_path / "db.sqlite3"))
    today = local_now("UTC").date()
    botmod.storage.ensure_user(1, "UTC", "00:00")
    botmod.storage.add_event(1, "Отпуск", today + timedelta(days=3))
    botmod.storage.add_event(1, "Старое", today - timedelta(days=1))

    fake = FakeBot()
    asyncio.run(botmod.send_daily(fake))
    asyncio.run(botmod.send_daily(fake))  # повтор в тот же день — без дубля

    to_user1 = [t for c, t in fake.sent if c == 1]
    assert len(to_user1) == 1
    assert "<b>3</b> дня до «Отпуск»" in to_user1[0]
    assert "Старое" not in to_user1[0]
    assert [e.title for e in botmod.storage.events(1)] == ["Отпуск"]
