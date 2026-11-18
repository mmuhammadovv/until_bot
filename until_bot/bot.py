"""Телеграм-бот: каждый день присылает карточку с тем, сколько дней осталось до выбранных дат."""
from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import date, time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

from .db import Event, Storage
from .render import MILESTONES, render_card, render_grid, ru_date
from .utils import local_now, parse_date, parse_time, parse_tz, plural_days

load_dotenv()

DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/Moscow")
DEFAULT_TIME = os.getenv("DEFAULT_TIME", "09:00")
DB_PATH = os.getenv("DB_PATH", "until_bot.sqlite3")
CHECK_INTERVAL = 30  # секунд между проверками расписания
MAX_EVENTS = 50
SEND_DELAY = 0.05  # пауза между рассылками разным пользователям
MAX_TITLE = 60

log = logging.getLogger("until_bot")
router = Router()
storage: Storage  # инициализируется в main()

# chat_id → id сообщения-«экрана», который бот редактирует вместо отправки новых (чистый чат).
screens: dict[int, int] = {}
# Карточки с отсчётом не удаляются, когда пользователь уходит в другое меню: у них только
# убираются кнопки, а подпись возвращается к обычной. (chat_id, message_id) → эта подпись.
cards: dict[tuple[int, int], str] = {}


# ---------------------------------------------------------------- тексты и клавиатуры

BTN_ADD, BTN_LIST, BTN_SETTINGS = "Добавить", "Мои даты", "Настройки"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_LIST)],
              [KeyboardButton(text=BTN_SETTINGS)]],
    resize_keyboard=True,
    is_persistent=True,
)

TIMEZONES = [
    ("Ташкент", "Asia/Tashkent"),
    ("Москва", "Europe/Moscow"),
    ("Астана", "Asia/Almaty"),
    ("Бишкек", "Asia/Bishkek"),
    ("Душанбе", "Asia/Dushanbe"),
    ("Ашхабад", "Asia/Ashgabat"),
    ("Баку", "Asia/Baku"),
    ("Киев", "Europe/Kyiv"),
    ("Минск", "Europe/Minsk"),
    ("Стамбул", "Europe/Istanbul"),
    ("Берлин", "Europe/Berlin"),
    ("Лондон", "Europe/London"),
    ("Екатеринбург", "Asia/Yekaterinburg"),
    ("Новосибирск", "Asia/Novosibirsk"),
    ("Владивосток", "Asia/Vladivostok"),
    ("Нью-Йорк", "America/New_York"),
]
TZ_LABELS = {name: label for label, name in TIMEZONES}
TZ_FLAGS = {
    "Asia/Tashkent": "🇺🇿", "Europe/Moscow": "🇷🇺", "Asia/Almaty": "🇰🇿", "Asia/Bishkek": "🇰🇬",
    "Asia/Dushanbe": "🇹🇯", "Asia/Ashgabat": "🇹🇲", "Asia/Baku": "🇦🇿", "Europe/Kyiv": "🇺🇦",
    "Europe/Minsk": "🇧🇾", "Europe/Istanbul": "🇹🇷", "Europe/Berlin": "🇩🇪", "Europe/London": "🇬🇧",
    "Asia/Yekaterinburg": "🇷🇺", "Asia/Novosibirsk": "🇷🇺", "Asia/Vladivostok": "🇷🇺",
    "America/New_York": "🇺🇸",
}
THEME_LABELS = {"dark": "Тёмная", "light": "Светлая"}

DESCRIPTION = (
    "Каждый день присылаю карточку с тем, сколько дней осталось до важных для тебя дат: "
    "дня рождения, отпуска, экзамена, Нового года.\n\nНажми «Начать»."
)
SHORT_DESCRIPTION = "Обратный отсчёт до важных дат — каждый день, с прогрессом."


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def days_short(n: int) -> str:
    return "сегодня" if n == 0 else f"{n} {plural_days(n)}"


class AddFlow(StatesGroup):
    title = State()
    when = State()


class EditFlow(StatesGroup):
    title = State()
    when = State()


class SettingsFlow(StatesGroup):
    time = State()
    tz = State()


# ---------------------------------------------------------------- «экраны» (чистый чат)

async def drop(message: Message) -> None:
    """Удаляет сообщение пользователя (кнопку меню, ответ на вопрос), чтобы чат оставался чистым."""
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def _delete_screen(bot: Bot, chat_id: int) -> None:
    old = screens.pop(chat_id, None)
    if old and (chat_id, old) in cards:
        # Карточку оставляем в чате, убираем только кнопки.
        caption = cards.pop((chat_id, old))
        try:
            await bot.edit_message_caption(chat_id=chat_id, message_id=old, caption=caption,
                                           reply_markup=None)
        except TelegramBadRequest:
            pass
        return
    if old:
        try:
            await bot.delete_message(chat_id, old)
        except TelegramBadRequest:
            pass


def _is_screen(msg: Message | None) -> bool:
    return isinstance(msg, Message) and screens.get(msg.chat.id) == msg.message_id


async def show_text(bot: Bot, chat_id: int, text: str, markup: InlineKeyboardMarkup | None,
                    edit: Message | None = None) -> None:
    """Показывает текстовый экран: редактирует текущий, если можно, иначе заменяет его новым."""
    if _is_screen(edit) and not edit.photo:  # type: ignore[union-attr]
        try:
            await edit.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
    await _delete_screen(bot, chat_id)
    msg = await bot.send_message(chat_id, text, reply_markup=markup)
    screens[chat_id] = msg.message_id


async def show_photo(bot: Bot, chat_id: int, png: bytes, caption: str,
                     markup: InlineKeyboardMarkup | None, edit: Message | None = None,
                     keep: str | None = None) -> None:
    """Экран-карточка. keep — подпись, которая останется у карточки, когда пользователь уйдёт дальше."""
    photo = BufferedInputFile(png, "card.png")
    if _is_screen(edit) and edit.photo:  # type: ignore[union-attr]
        try:
            await edit.edit_media(  # type: ignore[union-attr]
                InputMediaPhoto(media=photo, caption=caption), reply_markup=markup
            )
            cards[(chat_id, edit.message_id)] = keep or caption  # type: ignore[union-attr]
            return
        except TelegramBadRequest:
            pass
    await _delete_screen(bot, chat_id)
    msg = await bot.send_photo(chat_id, photo, caption=caption, reply_markup=markup)
    screens[chat_id] = msg.message_id
    cards[(chat_id, msg.message_id)] = keep or caption


async def set_caption(bot: Bot, chat_id: int, caption: str, markup: InlineKeyboardMarkup | None,
                      edit: Message | None) -> None:
    """Меняет только подпись и кнопки у экрана-карточки (подтверждение удаления, меню изменения)."""
    if _is_screen(edit) and edit.photo:  # type: ignore[union-attr]
        try:
            await edit.edit_caption(caption=caption, reply_markup=markup)  # type: ignore[union-attr]
            return
        except TelegramBadRequest:
            pass
    await show_text(bot, chat_id, caption, markup)


async def screen_text(bot: Bot, chat_id: int, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """Меняет текст текущего экрана, какого бы типа он ни был (используется в пошаговых вопросах)."""
    mid = screens.get(chat_id)
    if mid:
        # Экран может быть текстом или карточкой — пробуем оба способа.
        attempts = (
            lambda: bot.edit_message_text(text=text, chat_id=chat_id, message_id=mid, reply_markup=markup),
            lambda: bot.edit_message_caption(caption=text, chat_id=chat_id, message_id=mid, reply_markup=markup),
        )
        for attempt in attempts:
            try:
                await attempt()
                return
            except TelegramBadRequest as e:
                if "not modified" in str(e):
                    return
    await show_text(bot, chat_id, text, markup)


# ---------------------------------------------------------------- экраны

def user_today(chat_id: int) -> date:
    user = storage.ensure_user(chat_id, DEFAULT_TZ, DEFAULT_TIME)
    return local_now(user.tz).date()


def theme_of(chat_id: int) -> str:
    return storage.ensure_user(chat_id, DEFAULT_TZ, DEFAULT_TIME).theme


async def open_list(bot: Bot, chat_id: int, edit: Message | None = None) -> None:
    today = user_today(chat_id)
    events = storage.events(chat_id)
    if not events:
        await show_text(
            bot, chat_id,
            "<b>Мои даты</b>\n\nПока пусто. Добавь первую дату — например, день рождения или отпуск.",
            kb([btn("Добавить дату", "add")]), edit,
        )
        return
    rows = [[btn(f"{e.title} · {days_short((e.date - today).days)}", f"ev:{e.id}")] for e in events]
    rows.append([btn("Добавить дату", "add")])
    await show_text(bot, chat_id, "<b>Мои даты</b>\n\nНажми на дату, чтобы открыть карточку.",
                    kb(*rows), edit)


def card_caption(e: Event, today: date) -> str:
    days = (e.date - today).days
    tail = MILESTONES.get(days, "Сегодня" if days == 0 else days_short(days))
    return f"<b>{html.escape(e.title)}</b>\n{ru_date(e.date)} · {tail.lower() if days else tail}"


def card_kb(e: Event) -> InlineKeyboardMarkup:
    return kb([btn("Изменить", f"edit:{e.id}"), btn("Удалить", f"delask:{e.id}")],
              [btn("‹ Назад", "list")])


async def open_card(bot: Bot, chat_id: int, e: Event, edit: Message | None = None,
                    caption: str | None = None) -> None:
    today = user_today(chat_id)
    png = render_card(e.title, e.created, e.date, today, theme_of(chat_id))
    clean = card_caption(e, today)
    await show_photo(bot, chat_id, png, caption or clean, card_kb(e), edit, keep=clean)


def settings_view(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    u = storage.ensure_user(chat_id, DEFAULT_TZ, DEFAULT_TIME)
    tz_label = TZ_LABELS.get(u.tz, u.tz)
    text = (
        "<b>Настройки</b>\n\n"
        f"Каждый день в <b>{u.notify_time}</b> ({html.escape(tz_label)}) "
        "я присылаю карточку с отсчётом."
    )
    markup = kb(
        [btn(f"Время уведомления · {u.notify_time}", "set:time")],
        [btn(f"Часовой пояс · {tz_label}", "set:tz")],
        [btn(f"Тема карточек · {THEME_LABELS.get(u.theme, u.theme)}", "set:theme")],
        [btn("Удалить все мои данные", "set:wipe")],
    )
    return text, markup


def tz_kb(onboarding: bool) -> InlineKeyboardMarkup:
    suffix = ":o" if onboarding else ""
    buttons = [btn(f"{TZ_FLAGS.get(name, '')} {label}".strip(), f"tz:{name}{suffix}")
               for label, name in TIMEZONES]
    rows = [buttons[i: i + 2] for i in range(0, len(buttons), 2)]
    rows.append([btn("Другой часовой пояс", f"tzc{suffix}")])
    if not onboarding:
        rows.append([btn("‹ Назад", "settings")])
    return kb(*rows)


def time_kb(onboarding: bool) -> InlineKeyboardMarkup:
    suffix = ":o" if onboarding else ""
    buttons = [btn(f"{h:02d}:00", f"time:{h:02d}.00{suffix}") for h in range(24)]
    rows = [buttons[i: i + 6] for i in range(0, 24, 6)]
    rows.append([btn("Другое время, например 08:30", f"timec{suffix}")])
    if not onboarding:
        rows.append([btn("‹ Назад", "settings")])
    return kb(*rows)


TZ_PROMPT = "Выбери город с таким же временем, как у тебя."
TIME_PROMPT = "Во сколько каждый день присылать карточку?"


# ---------------------------------------------------------------- старт и меню

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    storage.ensure_user(message.chat.id, DEFAULT_TZ, DEFAULT_TIME)
    await message.answer(
        "Привет! Я каждый день присылаю карточку с тем, сколько дней осталось до важных для тебя дат.\n\n"
        "Меню — на кнопках внизу.",
        reply_markup=MAIN_KB,
    )
    await show_text(bot, message.chat.id, f"<b>Шаг 1 из 2.</b> Часовой пояс\n\n{TZ_PROMPT}", tz_kb(True))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Добавить</b> — новая дата для отсчёта.\n"
        "<b>Мои даты</b> — карточки, изменение и удаление.\n"
        "<b>Настройки</b> — время уведомления, часовой пояс, тема.\n\n"
        "Дату можно добавить и одним сообщением: <code>31.12.2026 Новый год</code>",
        reply_markup=MAIN_KB,
    )


@router.message(F.text == BTN_LIST)
@router.message(Command("list"))
async def menu_list(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await drop(message)
    await open_list(bot, message.chat.id)


@router.message(F.text == BTN_SETTINGS)
@router.message(Command("settings"))
async def menu_settings(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await drop(message)
    await show_text(bot, message.chat.id, *settings_view(message.chat.id))


@router.message(F.text == BTN_ADD)
@router.message(Command("add"))
async def menu_add(message: Message, state: FSMContext, bot: Bot) -> None:
    await drop(message)
    await start_add(bot, message.chat.id, state)


async def start_add(bot: Bot, chat_id: int, state: FSMContext, edit: Message | None = None) -> None:
    if len(storage.events(chat_id)) >= MAX_EVENTS:
        await show_text(bot, chat_id, f"Можно хранить не больше {MAX_EVENTS} дат. Удали лишние.",
                        kb([btn("Мои даты", "list")]), edit)
        return
    await state.set_state(AddFlow.title)
    await show_text(bot, chat_id, "<b>Новая дата</b>\n\nКак назовём событие?",
                    kb([btn("Отмена", "cancel")]), edit)


@router.callback_query(F.data == "add")
async def cb_add(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await query.answer()
    await start_add(bot, query.from_user.id, state, query.message)  # type: ignore[arg-type]


@router.callback_query(F.data == "list")
async def cb_list(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await query.answer()
    await open_list(bot, query.from_user.id, query.message)  # type: ignore[arg-type]


@router.callback_query(F.data == "cancel")
async def cb_cancel(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await query.answer("Отменено")
    await open_list(bot, query.from_user.id, query.message)  # type: ignore[arg-type]


# ---------------------------------------------------------------- добавление

DATE_HINT = "Напиши дату: <code>31.12.2026</code> или <code>31.12</code> (тогда возьму ближайшее)."


@router.message(AddFlow.title, F.text)
async def add_title(message: Message, state: FSMContext, bot: Bot) -> None:
    await drop(message)
    title = (message.text or "").strip()[:MAX_TITLE]
    if not title:
        return
    await state.update_data(title=title)
    await state.set_state(AddFlow.when)
    await screen_text(bot, message.chat.id,
                      f"<b>Новая дата</b> · {html.escape(title)}\n\nКогда? {DATE_HINT}",
                      kb([btn("Отмена", "cancel")]))


def check_date(text: str, today: date) -> tuple[date | None, str | None]:
    try:
        when = parse_date(text, today)
    except ValueError:
        return None, "Не получилось разобрать дату."
    if when < today:
        return None, "Эта дата уже прошла."
    return when, None


@router.message(AddFlow.when, F.text)
async def add_when(message: Message, state: FSMContext, bot: Bot) -> None:
    await drop(message)
    chat_id = message.chat.id
    title = (await state.get_data()).get("title", "Событие")
    today = user_today(chat_id)
    when, err = check_date(message.text or "", today)
    if err:
        await screen_text(bot, chat_id,
                          f"<b>Новая дата</b> · {html.escape(title)}\n\n{err} {DATE_HINT}",
                          kb([btn("Отмена", "cancel")]))
        return
    await state.clear()
    event_id = storage.add_event(chat_id, title, when, today)  # type: ignore[arg-type]
    e = storage.get_event(chat_id, event_id)
    await open_card(bot, chat_id, e, caption="Добавлено\n\n" + card_caption(e, today))  # type: ignore[arg-type]


# ---------------------------------------------------------------- карточка, изменение, удаление

def _event_from(query: CallbackQuery) -> Event | None:
    try:
        event_id = int(query.data.rsplit(":", 1)[1])  # type: ignore[union-attr]
    except (ValueError, IndexError):
        return None
    return storage.get_event(query.from_user.id, event_id)


@router.callback_query(F.data.startswith("ev:"))
async def cb_event(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    e = _event_from(query)
    await query.answer()
    if e is None:
        await open_list(bot, query.from_user.id, query.message)  # type: ignore[arg-type]
        return
    await open_card(bot, query.from_user.id, e, query.message)  # type: ignore[arg-type]


@router.callback_query(F.data.startswith("delask:"))
async def cb_delete_ask(query: CallbackQuery, bot: Bot) -> None:
    e = _event_from(query)
    await query.answer()
    if e is None:
        return
    await set_caption(bot, query.from_user.id, f"Удалить «{html.escape(e.title)}»?",
                      kb([btn("Да, удалить", f"del:{e.id}"), btn("Нет", f"ev:{e.id}")]),
                      query.message)  # type: ignore[arg-type]


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(query: CallbackQuery, bot: Bot) -> None:
    e = _event_from(query)
    chat_id = query.from_user.id
    if e is not None:
        storage.delete_event(chat_id, e.id)
    await query.answer("Удалено")
    # Карточку удалённой даты убираем из чата насовсем.
    if _is_screen(query.message):
        mid = screens.pop(chat_id)
        cards.pop((chat_id, mid), None)
        try:
            await bot.delete_message(chat_id, mid)
        except TelegramBadRequest:
            pass
    await open_list(bot, query.from_user.id, query.message)  # type: ignore[arg-type]


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(query: CallbackQuery, bot: Bot) -> None:
    e = _event_from(query)
    await query.answer()
    if e is None:
        return
    await set_caption(bot, query.from_user.id, f"Что изменить в «{html.escape(e.title)}»?",
                      kb([btn("Название", f"edt:{e.id}"), btn("Дату", f"edd:{e.id}")],
                         [btn("‹ Назад", f"ev:{e.id}")]),
                      query.message)  # type: ignore[arg-type]


@router.callback_query(F.data.startswith("edt:") | F.data.startswith("edd:"))
async def cb_edit_field(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    e = _event_from(query)
    await query.answer()
    if e is None:
        return
    is_title = query.data.startswith("edt:")  # type: ignore[union-attr]
    await state.set_state(EditFlow.title if is_title else EditFlow.when)
    await state.update_data(event_id=e.id)
    prompt = "Напиши новое название." if is_title else f"Напиши новую дату. {DATE_HINT}"
    await set_caption(bot, query.from_user.id, f"<b>{html.escape(e.title)}</b>\n\n{prompt}",
                      kb([btn("Отмена", f"ev:{e.id}")]), query.message)  # type: ignore[arg-type]


@router.message(StateFilter(EditFlow.title, EditFlow.when), F.text)
async def edit_value(message: Message, state: FSMContext, bot: Bot) -> None:
    await drop(message)
    chat_id = message.chat.id
    e = storage.get_event(chat_id, (await state.get_data()).get("event_id", -1))
    if e is None:
        await state.clear()
        await open_list(bot, chat_id)
        return
    if await state.get_state() == EditFlow.title.state:
        title = (message.text or "").strip()[:MAX_TITLE]
        if not title:
            return
        storage.update_event(chat_id, e.id, title=title)
    else:
        today = user_today(chat_id)
        when, err = check_date(message.text or "", today)
        if err:
            await screen_text(bot, chat_id, f"<b>{html.escape(e.title)}</b>\n\n{err} {DATE_HINT}",
                              kb([btn("Отмена", f"ev:{e.id}")]))
            return
        storage.update_event(chat_id, e.id, when=when)
    await state.clear()
    e = storage.get_event(chat_id, e.id)
    mid = screens.get(chat_id)
    # Перерисовываем ту же карточку на месте.
    if mid:
        try:
            today = user_today(chat_id)
            png = render_card(e.title, e.created, e.date, today, theme_of(chat_id))  # type: ignore[union-attr]
            await bot.edit_message_media(
                InputMediaPhoto(media=BufferedInputFile(png, "card.png"),
                                caption="Сохранено\n\n" + card_caption(e, today)),  # type: ignore[arg-type]
                chat_id=chat_id, message_id=mid, reply_markup=card_kb(e),  # type: ignore[arg-type]
            )
            cards[(chat_id, mid)] = card_caption(e, today)  # type: ignore[arg-type]
            return
        except TelegramBadRequest:
            pass
    await open_card(bot, chat_id, e)  # type: ignore[arg-type]


# ---------------------------------------------------------------- настройки

@router.callback_query(F.data == "settings")
async def cb_settings(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await query.answer()
    await show_text(bot, query.from_user.id, *settings_view(query.from_user.id), query.message)  # type: ignore[arg-type]


@router.callback_query(F.data == "set:time")
async def cb_set_time(query: CallbackQuery, bot: Bot) -> None:
    await query.answer()
    await show_text(bot, query.from_user.id, f"<b>Время уведомления</b>\n\n{TIME_PROMPT}",
                    time_kb(False), query.message)  # type: ignore[arg-type]


@router.callback_query(F.data == "set:tz")
async def cb_set_tz(query: CallbackQuery, bot: Bot) -> None:
    await query.answer()
    await show_text(bot, query.from_user.id, f"<b>Часовой пояс</b>\n\n{TZ_PROMPT}",
                    tz_kb(False), query.message)  # type: ignore[arg-type]


@router.callback_query(F.data == "set:theme")
async def cb_set_theme(query: CallbackQuery, bot: Bot) -> None:
    chat_id = query.from_user.id
    new = "light" if theme_of(chat_id) == "dark" else "dark"
    storage.set_user_field(chat_id, "theme", new)
    await query.answer(f"Тема: {THEME_LABELS[new].lower()}")
    await show_text(bot, chat_id, *settings_view(chat_id), query.message)  # type: ignore[arg-type]


@router.callback_query(F.data == "set:wipe")
async def cb_wipe_ask(query: CallbackQuery, bot: Bot) -> None:
    await query.answer()
    await show_text(bot, query.from_user.id,
                    "Удалить все твои даты и настройки? Это нельзя отменить.",
                    kb([btn("Да, удалить всё", "set:wipe!"), btn("Нет", "settings")]),
                    query.message)  # type: ignore[arg-type]


@router.callback_query(F.data == "set:wipe!")
async def cb_wipe(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    storage.delete_user(query.from_user.id)
    await query.answer("Удалено")
    await show_text(bot, query.from_user.id,
                    "Все данные удалены. Чтобы начать заново, отправь /start.", None, query.message)  # type: ignore[arg-type]


def apply_time(chat_id: int, t: time) -> None:
    user = storage.ensure_user(chat_id, DEFAULT_TZ, DEFAULT_TIME)
    storage.set_user_field(chat_id, "notify_time", t.strftime("%H:%M"))
    # Если новое время сегодня ещё не наступило — пришлём карточку и сегодня.
    if local_now(user.tz).time() < t:
        storage.set_user_field(chat_id, "last_sent", None)


def apply_tz(chat_id: int, name: str) -> None:
    storage.ensure_user(chat_id, DEFAULT_TZ, DEFAULT_TIME)
    storage.set_user_field(chat_id, "tz", name)


async def finish_onboarding(bot: Bot, chat_id: int, edit: Message | None) -> None:
    u = storage.ensure_user(chat_id, DEFAULT_TZ, DEFAULT_TIME)
    await show_text(
        bot, chat_id,
        f"<b>Готово.</b> Каждый день в {u.notify_time} буду присылать карточку с отсчётом.\n\n"
        "Осталось добавить первую дату.",
        kb([btn("Добавить дату", "add")]), edit,
    )


@router.callback_query(F.data.startswith("tz:"))
async def cb_tz(query: CallbackQuery, bot: Bot) -> None:
    parts = query.data.split(":")  # type: ignore[union-attr]
    name, onboarding = parts[1], len(parts) > 2
    await query.answer()
    if name not in TZ_LABELS:
        return
    chat_id = query.from_user.id
    apply_tz(chat_id, name)
    if onboarding:
        await show_text(bot, chat_id, f"<b>Шаг 2 из 2.</b> Время\n\n{TIME_PROMPT}",
                        time_kb(True), query.message)  # type: ignore[arg-type]
    else:
        await show_text(bot, chat_id, *settings_view(chat_id), query.message)  # type: ignore[arg-type]


@router.callback_query(F.data.startswith("time:"))
async def cb_time(query: CallbackQuery, bot: Bot) -> None:
    parts = query.data.split(":")  # type: ignore[union-attr]
    await query.answer()
    try:
        t = parse_time(parts[1])
    except ValueError:
        return
    chat_id = query.from_user.id
    apply_time(chat_id, t)
    if len(parts) > 2:
        await finish_onboarding(bot, chat_id, query.message)  # type: ignore[arg-type]
    else:
        await show_text(bot, chat_id, *settings_view(chat_id), query.message)  # type: ignore[arg-type]


@router.callback_query(F.data.startswith("tzc") | F.data.startswith("timec"))
async def cb_custom(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = query.data or ""
    onboarding = data.endswith(":o")
    await query.answer()
    back = kb([btn("‹ Назад", "settings")]) if not onboarding else None
    if data.startswith("tzc"):
        await state.set_state(SettingsFlow.tz)
        text = ("<b>Часовой пояс</b>\n\nНапиши название пояса или смещение от UTC, например "
                "<code>Asia/Samarkand</code> или <code>+5</code>.")
    else:
        await state.set_state(SettingsFlow.time)
        text = "<b>Время уведомления</b>\n\nНапиши время, например <code>08:30</code>."
    await state.update_data(onboarding=onboarding)
    await show_text(bot, query.from_user.id, text, back, query.message)  # type: ignore[arg-type]


@router.message(StateFilter(SettingsFlow.tz, SettingsFlow.time), F.text)
async def custom_value(message: Message, state: FSMContext, bot: Bot) -> None:
    await drop(message)
    chat_id = message.chat.id
    onboarding = (await state.get_data()).get("onboarding", False)
    value = (message.text or "").strip()
    if await state.get_state() == SettingsFlow.tz.state:
        try:
            parse_tz(value)
        except ValueError:
            await screen_text(bot, chat_id, "Не знаю такой часовой пояс. Пример: <code>Asia/Samarkand</code> "
                              "или <code>+5</code>.", None if onboarding else kb([btn("‹ Назад", "settings")]))
            return
        apply_tz(chat_id, value)
        await state.clear()
        if onboarding:
            await show_text(bot, chat_id, f"<b>Шаг 2 из 2.</b> Время\n\n{TIME_PROMPT}", time_kb(True))
            return
    else:
        try:
            t = parse_time(value)
        except ValueError:
            await screen_text(bot, chat_id, "Не получилось. Напиши время так: <code>08:30</code>.",
                              None if onboarding else kb([btn("‹ Назад", "settings")]))
            return
        apply_time(chat_id, t)
        await state.clear()
        if onboarding:
            await finish_onboarding(bot, chat_id, None)
            return
    await show_text(bot, chat_id, *settings_view(chat_id))


# ---------------------------------------------------------------- быстрый ввод одной строкой

@router.message(F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith("/"))
async def quick_add(message: Message, bot: Bot) -> None:
    """«31.12.2026 Новый год» без меню — сразу добавляем."""
    chat_id = message.chat.id
    parts = (message.text or "").strip().split(maxsplit=1)
    today = user_today(chat_id)
    when = None
    if len(parts) == 2:
        when, _ = check_date(parts[0], today)
    await drop(message)
    if when is None:
        await show_text(bot, chat_id,
                        "Чтобы добавить дату, нажми «Добавить» внизу или напиши одной строкой: "
                        "<code>31.12.2026 Новый год</code>",
                        kb([btn("Добавить дату", "add")]))
        return
    if len(storage.events(chat_id)) >= MAX_EVENTS:
        await show_text(bot, chat_id, f"Можно хранить не больше {MAX_EVENTS} дат.", kb([btn("Мои даты", "list")]))
        return
    event_id = storage.add_event(chat_id, parts[1].strip()[:MAX_TITLE], when, today)
    e = storage.get_event(chat_id, event_id)
    await open_card(bot, chat_id, e, caption="Добавлено\n\n" + card_caption(e, today))  # type: ignore[arg-type]


# ---------------------------------------------------------------- ежедневная рассылка

DAILY_KB = kb([btn("Все даты", "list"), btn("Добавить", "add")])


def daily_caption(events: list[Event], today: date) -> str:
    lines = []
    for e in events:
        days = (e.date - today).days
        if days == 0:
            lines.append(f"<b>{html.escape(e.title)} — сегодня</b>")
        elif days in MILESTONES:
            lines.append(f"<b>{html.escape(e.title)} — {MILESTONES[days].lower()}</b>")
        else:
            lines.append(f"{html.escape(e.title)} — {days_short(days)}")
    text = "\n".join(lines)
    return text if len(text) <= 1000 else text[:997] + "…"


async def send_with_retry(bot: Bot, chat_id: int, png: bytes, caption: str) -> None:
    for _ in range(3):
        try:
            await bot.send_photo(chat_id, BufferedInputFile(png, "countdown.png"),
                                 caption=caption, reply_markup=DAILY_KB)
            return
        except TelegramRetryAfter as e:
            log.warning("Лимит Telegram, жду %s с", e.retry_after)
            await asyncio.sleep(e.retry_after)
    await bot.send_photo(chat_id, BufferedInputFile(png, "countdown.png"),
                         caption=caption, reply_markup=DAILY_KB)


async def send_daily(bot: Bot) -> None:
    """Один проход: отправить карточку всем, у кого наступило время и кому сегодня ещё не писали."""
    for user in storage.all_users():
        try:
            now = local_now(user.tz)
            today = now.date()
            if user.last_sent == today.isoformat() or now.time() < parse_time(user.notify_time):
                continue
            storage.delete_past(user.chat_id, today)  # прошедшие даты больше не нужны
            events = storage.events(user.chat_id)
            if events:
                png = await asyncio.to_thread(
                    render_grid, [(e.title, e.created, e.date) for e in events], today, user.theme
                )
                await send_with_retry(bot, user.chat_id, png, daily_caption(events, today))
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


async def setup_profile(bot: Bot) -> None:
    """Описание бота и меню команд (оформление профиля). Аватарку ставят через @BotFather."""
    await bot.set_my_commands([
        BotCommand(command="list", description="Мои даты"),
        BotCommand(command="add", description="Добавить дату"),
        BotCommand(command="settings", description="Настройки"),
        BotCommand(command="start", description="Начать заново"),
    ])
    try:
        await bot.set_my_description(DESCRIPTION)
        await bot.set_my_short_description(SHORT_DESCRIPTION)
    except TelegramBadRequest as e:
        log.warning("Не удалось обновить описание бота: %s", e)


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
    await setup_profile(bot)
    task = asyncio.create_task(scheduler(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
