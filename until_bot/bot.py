"""Телеграм-бот: каждый день присылает, сколько дней осталось до выбранных дат."""
from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import date

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

from .db import Storage
from .utils import describe, local_now, parse_date, parse_time, parse_tz

load_dotenv()

DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/Moscow")
DEFAULT_TIME = os.getenv("DEFAULT_TIME", "09:00")
DB_PATH = os.getenv("DB_PATH", "until_bot.sqlite3")
CHECK_INTERVAL = 30  # секунд между проверками расписания
MAX_EVENTS = 50
SEND_DELAY = 0.05  # пауза между рассылками разным пользователям
MAX_TITLE = 100

HELP = (
    "Я каждый день напомню, сколько дней осталось до важных для тебя дат.\n\n"
    "<b>Добавить дату</b> — просто напиши:\n"
    "<code>31.12.2026 Новый год</code>\n"
    "<code>15.06 День рождения мамы</code> (год можно не писать)\n"
    "или <code>/add 01.09.2027 Отпуск</code>\n\n"
    "/list — мои даты (там же можно удалить)\n"
    "/now — прислать отсчёт прямо сейчас\n"
    "/time 08:30 — во сколько присылать сообщение\n"
    "/tz Europe/Moscow или /tz +5 — часовой пояс\n"
    "/settings — текущие настройки\n"
    "/stop — удалить все мои данные"
)

log = logging.getLogger("until_bot")
router = Router()
storage: Storage  # инициализируется в main()


def _today(chat_id: int) -> date:
    user = storage.ensure_user(chat_id, DEFAULT_TZ, DEFAULT_TIME)
    return local_now(user.tz).date()


def build_report(chat_id: int, today: date) -> str | None:
    events = storage.events(chat_id)
    if not events:
        return None
    lines = [describe(html.escape(e.title), e.date, today) for e in events]
    return "⏳ <b>Обратный отсчёт</b>\n\n" + "\n".join(lines)


def list_keyboard(chat_id: int) -> InlineKeyboardMarkup | None:
    events = storage.events(chat_id)
    if not events:
        return None
    rows = [
        [
            InlineKeyboardButton(
                text=f"🗑 {e.date:%d.%m.%Y} {e.title}"[:60], callback_data=f"del:{e.id}"
            )
        ]
        for e in events
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def add_event(message: Message, text: str) -> None:
    chat_id = message.chat.id
    today = _today(chat_id)
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Напиши дату и название, например:\n<code>31.12.2026 Новый год</code>"
        )
        return
    try:
        when = parse_date(parts[0], today)
    except ValueError:
        await message.answer(
            "Не понял дату 🤔 Формат: <code>ДД.ММ.ГГГГ Название</code>, "
            "например <code>31.12.2026 Новый год</code>.\n/help — справка"
        )
        return
    if when < today:
        await message.answer("Эта дата уже прошла — выбери дату в будущем.")
        return
    title = parts[1].strip()[:MAX_TITLE]
    if len(storage.events(chat_id)) >= MAX_EVENTS:
        await message.answer(f"Можно хранить не больше {MAX_EVENTS} дат. Удали лишние в /list.")
        return
    storage.add_event(chat_id, title, when)
    await message.answer("✅ Добавлено:\n" + describe(html.escape(title), when, today))


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = storage.ensure_user(message.chat.id, DEFAULT_TZ, DEFAULT_TIME)
    await message.answer(
        f"Привет! 👋\n\n{HELP}\n\n"
        f"Сейчас сообщения приходят в <b>{user.notify_time}</b> ({html.escape(user.tz)})."
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject) -> None:
    await add_event(message, command.args or "")


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    chat_id = message.chat.id
    report = build_report(chat_id, _today(chat_id))
    if report is None:
        await message.answer("Дат пока нет. Напиши, например: <code>31.12.2026 Новый год</code>")
        return
    await message.answer(
        report + "\n\nНажми на кнопку, чтобы удалить дату:", reply_markup=list_keyboard(chat_id)
    )


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(query: CallbackQuery) -> None:
    chat_id = query.message.chat.id if query.message else query.from_user.id
    try:
        event_id = int(query.data.split(":", 1)[1])  # type: ignore[union-attr]
    except ValueError:
        await query.answer()
        return
    deleted = storage.delete_event(chat_id, event_id)
    await query.answer("Удалено" if deleted else "Уже удалено")
    if query.message:
        report = build_report(chat_id, _today(chat_id))
        if report is None:
            await query.message.edit_text("Дат больше нет.")  # type: ignore[union-attr]
        else:
            await query.message.edit_text(  # type: ignore[union-attr]
                report + "\n\nНажми на кнопку, чтобы удалить дату:",
                reply_markup=list_keyboard(chat_id),
            )


@router.message(Command("now"))
async def cmd_now(message: Message) -> None:
    chat_id = message.chat.id
    report = build_report(chat_id, _today(chat_id))
    await message.answer(report or "Дат пока нет. /help — как добавить.")


@router.message(Command("time"))
async def cmd_time(message: Message, command: CommandObject) -> None:
    chat_id = message.chat.id
    user = storage.ensure_user(chat_id, DEFAULT_TZ, DEFAULT_TIME)
    try:
        t = parse_time(command.args or "")
    except ValueError:
        await message.answer("Укажи время в формате ЧЧ:ММ, например: <code>/time 08:30</code>")
        return
    value = t.strftime("%H:%M")
    storage.set_user_field(chat_id, "notify_time", value)
    # Если новое время сегодня ещё не наступило — пришлём сообщение и сегодня.
    now = local_now(user.tz)
    if now.time() < t:
        storage.set_user_field(chat_id, "last_sent", None)
    await message.answer(f"⏰ Теперь буду писать каждый день в <b>{value}</b>.")


@router.message(Command("tz"))
async def cmd_tz(message: Message, command: CommandObject) -> None:
    chat_id = message.chat.id
    storage.ensure_user(chat_id, DEFAULT_TZ, DEFAULT_TIME)
    arg = (command.args or "").strip()
    try:
        parse_tz(arg)
    except ValueError:
        await message.answer(
            "Укажи часовой пояс, например:\n<code>/tz Europe/Moscow</code>\n"
            "<code>/tz Asia/Tashkent</code>\n<code>/tz +3</code>"
        )
        return
    storage.set_user_field(chat_id, "tz", arg)
    now = local_now(arg)
    await message.answer(
        f"🌍 Часовой пояс: <b>{html.escape(arg)}</b>. Местное время сейчас {now:%H:%M}."
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    user = storage.ensure_user(message.chat.id, DEFAULT_TZ, DEFAULT_TIME)
    await message.answer(
        f"⏰ Время: <b>{user.notify_time}</b>\n🌍 Часовой пояс: <b>{html.escape(user.tz)}</b>\n"
        f"📅 Дат: {len(storage.events(user.chat_id))}"
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    storage.delete_user(message.chat.id)
    await message.answer("Все твои данные удалены. Напиши /start, чтобы начать заново.")


# Обычный текст считаем добавлением даты только в личке, чтобы не реагировать на переписку в группах.
@router.message(F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith("/"))
async def plain_text(message: Message) -> None:
    await add_event(message, message.text or "")


async def send_with_retry(bot: Bot, chat_id: int, text: str) -> None:
    for _ in range(3):
        try:
            await bot.send_message(chat_id, text)
            return
        except TelegramRetryAfter as e:
            log.warning("Лимит Telegram, жду %s с", e.retry_after)
            await asyncio.sleep(e.retry_after)
    await bot.send_message(chat_id, text)


async def send_daily(bot: Bot) -> None:
    """Один проход: отправить отсчёт всем, у кого наступило время и кому сегодня ещё не писали."""
    for user in storage.all_users():
        try:
            now = local_now(user.tz)
            today = now.date()
            if user.last_sent == today.isoformat() or now.time() < parse_time(user.notify_time):
                continue
            storage.delete_past(user.chat_id, today)  # прошедшие даты больше не нужны
            report = build_report(user.chat_id, today)
            if report:
                await send_with_retry(bot, user.chat_id, report)
                await asyncio.sleep(SEND_DELAY)  # не превышаем лимиты Telegram (~30 сообщений/с)
            storage.set_user_field(user.chat_id, "last_sent", today.isoformat())
        except TelegramForbiddenError:
            log.info("Пользователь %s заблокировал бота — удаляю данные", user.chat_id)
            storage.delete_user(user.chat_id)
        except Exception:
            log.exception("Не удалось отправить отсчёт пользователю %s", user.chat_id)


async def scheduler(bot: Bot) -> None:
    while True:
        await send_daily(bot)
        await asyncio.sleep(CHECK_INTERVAL)


async def main() -> None:
    global storage
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")
    parse_tz(DEFAULT_TZ)
    parse_time(DEFAULT_TIME)
    storage = Storage(DB_PATH)

    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands(
        [
            BotCommand(command="list", description="Мои даты"),
            BotCommand(command="add", description="Добавить дату"),
            BotCommand(command="now", description="Отсчёт прямо сейчас"),
            BotCommand(command="time", description="Время ежедневного сообщения"),
            BotCommand(command="tz", description="Часовой пояс"),
            BotCommand(command="settings", description="Настройки"),
            BotCommand(command="help", description="Справка"),
        ]
    )
    task = asyncio.create_task(scheduler(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
