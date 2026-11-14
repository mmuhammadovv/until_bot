"""Чистые вспомогательные функции: разбор дат, часовых поясов, склонения."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def plural_days(n: int) -> str:
    """1 день, 2 дня, 5 дней, 11 дней, 21 день..."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "дня"
    return "дней"


def parse_date(text: str, today: date) -> date:
    """Принимает ДД.ММ.ГГГГ, ДД.ММ.ГГ, ДД.ММ (ближайшая такая дата) и ГГГГ-ММ-ДД."""
    text = text.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text):
            return date.fromisoformat("-".join(p.zfill(2) for p in text.split("-")))
        parts = re.split(r"[./]", text)
        if len(parts) == 3:
            d, m, y = (int(p) for p in parts)
            if len(parts[2]) == 2:
                y += 2000
            return date(y, m, d)
        if len(parts) == 2:
            d, m = (int(p) for p in parts)
            candidate = date(today.year, m, d)
            if candidate < today:
                candidate = date(today.year + 1, m, d)
            return candidate
    except ValueError:
        pass
    raise ValueError(f"Не понимаю дату: {text!r}")


def parse_tz(text: str) -> tzinfo:
    """Принимает имя IANA (Europe/Moscow) или смещение (+3, UTC+5:30, -04:00)."""
    text = text.strip()
    m = _OFFSET_RE.match(text)
    if m:
        sign, hours, minutes = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        if hours > 14 or minutes >= 60:
            raise ValueError(f"Некорректное смещение: {text!r}")
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(-delta if sign == "-" else delta)
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Неизвестный часовой пояс: {text!r}") from None


def parse_time(text: str) -> time:
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", text.strip())
    if not m:
        raise ValueError(f"Не понимаю время: {text!r}")
    return time(int(m.group(1)), int(m.group(2)))


def local_now(tz_name: str) -> datetime:
    return datetime.now(parse_tz(tz_name))


def describe(title: str, target: date, today: date) -> str:
    days = (target - today).days
    when = target.strftime("%d.%m.%Y")
    if days > 0:
        return f"• <b>{days}</b> {plural_days(days)} до «{title}» ({when})"
    if days == 0:
        return f"• 🎉 «{title}» — сегодня!"
    return f"• «{title}» прошло {-days} {plural_days(days)} назад ({when})"
