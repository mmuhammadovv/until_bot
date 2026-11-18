"""Сквозные сценарии: настоящие обработчики aiogram, но вместо Telegram — заглушка."""
import asyncio
import datetime as dt
from itertools import count

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery, DeleteMessage, EditMessageCaption, EditMessageMedia,
    EditMessageText, SendMessage, SendPhoto,
)
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User

from until_bot import bot as botmod
from until_bot.db import Storage

CHAT = 42
USER = User(id=CHAT, is_bot=False, first_name="Test")
PCHAT = Chat(id=CHAT, type="private")


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.ids = count(1000)
        self.kinds = {}  # message_id → "photo" | "text", как помнит Telegram

    def _msg(self, text=None, photo=False, mid=None):
        mid = mid or next(self.ids)
        self.kinds.setdefault(mid, "photo" if photo else "text")
        return Message(
            message_id=mid, date=dt.datetime.now(), chat=PCHAT, text=text,
            photo=[PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)] if photo else None,
        )

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return self._msg(text=method.text)
        if isinstance(method, SendPhoto):
            return self._msg(photo=True)
        if isinstance(method, (EditMessageText, EditMessageCaption, EditMessageMedia)):
            want = "text" if isinstance(method, EditMessageText) else "photo"
            if self.kinds.get(method.message_id) != want:
                raise TelegramBadRequest(method=method, message="Bad Request: wrong message type")
        if isinstance(method, (EditMessageText,)):
            return self._msg(text=method.text, mid=method.message_id)
        if isinstance(method, (EditMessageCaption, EditMessageMedia)):
            return self._msg(photo=True, mid=method.message_id)
        if isinstance(method, (DeleteMessage, AnswerCallbackQuery)):
            return True
        return True

    async def stream_content(self, *a, **k):
        yield b""

    async def close(self):
        pass

    def last(self, kind):
        return next(c for c in reversed(self.calls) if isinstance(c, kind))


@pytest.fixture
def env(tmp_path):
    botmod.storage = Storage(str(tmp_path / "db.sqlite3"))
    botmod.screens.clear()
    botmod.cards.clear()
    session = FakeSession()
    bot = Bot("123:abc", session=session)
    dp = Dispatcher()
    dp.include_router(botmod.router)
    uid = count(1)
    mid = count(1)

    async def text(t):
        msg = Message(message_id=next(mid), date=dt.datetime.now(), chat=PCHAT, from_user=USER, text=t)
        await dp.feed_update(bot, Update(update_id=next(uid), message=msg))

    async def press(data, photo=False):
        screen = botmod.screens.get(CHAT)
        photo = session.kinds.get(screen) == "photo"
        msg = session._msg(text=None if photo else "x", photo=photo, mid=screen)
        q = CallbackQuery(id="q", from_user=USER, chat_instance="c", data=data, message=msg)
        await dp.feed_update(bot, Update(update_id=next(uid), callback_query=q))

    yield session, text, press
    # aiogram роутер нельзя подключить к двум диспетчерам — отцепляем
    botmod.router._parent_router = None


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_full_flow(env):
    session, text, press = env

    async def scenario():
        await text("/start")
        first = session.last(SendMessage)
        assert "Шаг 1 из 2" in first.text
        assert first.reply_markup.inline_keyboard[0][0].text == "🇺🇿 Ташкент"
        await press("tz:Asia/Tashkent:o")
        assert "Шаг 2 из 2" in session.last(EditMessageText).text
        await press("time:08.00:o")
        assert "Готово" in session.last(EditMessageText).text
        u = botmod.storage.get_user(CHAT)
        assert (u.tz, u.notify_time) == ("Asia/Tashkent", "08:00")

        # Добавление по шагам
        await press("add")
        assert "Как назовём" in session.last(EditMessageText).text
        await text("Новый год")
        await text("31.12.2099")
        assert "Добавлено" in session.last(SendPhoto).caption
        [e] = botmod.storage.events(CHAT)
        assert e.title == "Новый год" and e.date == dt.date(2099, 12, 31)

        # Карточка не удаляется при переходе в меню — только убираются кнопки
        card_id = botmod.screens[CHAT]
        await text(botmod.BTN_LIST)
        assert not any(isinstance(c, DeleteMessage) and c.message_id == card_id for c in session.calls)
        strip = session.last(EditMessageCaption)
        assert strip.message_id == card_id and strip.reply_markup is None
        assert "Добавлено" not in strip.caption

        # Список → карточка → изменение названия
        assert "Мои даты" in session.last(SendMessage).text
        await press(f"ev:{e.id}")
        await press(f"edit:{e.id}", photo=True)
        assert "Что изменить" in session.last(EditMessageCaption).caption
        await press(f"edt:{e.id}", photo=True)
        await text("НГ")
        assert botmod.storage.get_event(CHAT, e.id).title == "НГ"
        assert "Сохранено" in session.last(EditMessageMedia).media.caption

        # Настройки: тема и своё время
        await text(botmod.BTN_SETTINGS)
        await press("set:theme")
        assert botmod.storage.get_user(CHAT).theme == "light"
        await press("timec")
        await text("07:45")
        assert botmod.storage.get_user(CHAT).notify_time == "07:45"

        # Быстрое добавление одной строкой и удаление с подтверждением
        await text("01.01.2100 Век")
        assert len(botmod.storage.events(CHAT)) == 2
        await press(f"delask:{e.id}", photo=True)
        assert "Удалить" in session.last(EditMessageCaption).caption
        deleted_card = botmod.screens[CHAT]
        await press(f"del:{e.id}", photo=True)
        assert [x.title for x in botmod.storage.events(CHAT)] == ["Век"]
        assert session.last(DeleteMessage).message_id == deleted_card

        # Ошибка ввода даты при изменении — подсказка появляется на карточке
        [v] = botmod.storage.events(CHAT)
        await press(f"ev:{v.id}")
        await press(f"edd:{v.id}")
        await text("вчера")
        assert "Не получилось" in session.last(EditMessageCaption).caption
        await press(f"ev:{v.id}")

        # Кнопка «Все даты» под ежедневной карточкой открывает новый экран, а не правит карточку
        await press("list")
        assert "Мои даты" in session.last(SendMessage).text

        # Сообщения пользователя удаляются (чистый чат)
        assert any(isinstance(c, DeleteMessage) for c in session.calls)

    run(scenario())
