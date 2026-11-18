"""Рисование карточек обратного отсчёта (кольцо прогресса) в PNG."""
from __future__ import annotations

import io
import math
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .utils import plural_days

FONTS = Path(__file__).parent / "fonts"
SS = 2  # рисуем в 2 раза крупнее и уменьшаем — так края получаются гладкими

MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

# Особые дни: сколько осталось → подпись на карточке.
MILESTONES = {100: "Осталось 100 дней", 30: "Остался месяц", 7: "Осталась неделя", 1: "Уже завтра"}


@dataclass(frozen=True)
class Theme:
    bg: str
    track: str
    ring: str
    text: str
    muted: str
    faint: str
    accent: str


THEMES = {
    "dark": Theme("#111214", "#26282c", "#e8e8e8", "#ffffff", "#8a8d93", "#6b6e74", "#ff6b4a"),
    "light": Theme("#f6f5f2", "#e3e1dc", "#151515", "#151515", "#7d7d7d", "#9a9a9a", "#e8552f"),
}


@lru_cache(maxsize=64)
def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / f"Inter-{weight}.otf"), size * SS)


def ru_date(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def remaining_fraction(created: date, target: date, today: date) -> float:
    total = (target - created).days
    if total <= 0:
        return 0.0
    return min(1.0, max(0.0, (target - today).days / total))


def _fit(draw: ImageDraw.ImageDraw, text: str, weight: str, size: int, max_w: int):
    """Уменьшает шрифт (и в крайнем случае обрезает текст), чтобы строка влезла по ширине."""
    max_w *= SS
    while size > 14 and draw.textlength(text, font=_font(weight, size)) > max_w:
        size -= 2
    font = _font(weight, size)
    if draw.textlength(text, font=font) > max_w:
        while text and draw.textlength(text + "…", font=font) > max_w:
            text = text[:-1]
        text += "…"
    return text, font


def _draw_card(
    draw: ImageDraw.ImageDraw, x: int, y: int, size: int,
    title: str, created: date, target: date, today: date, t: Theme, footer: bool,
) -> None:
    """Одна карточка-кольцо в квадрате size×size с левым верхним углом (x, y). Координаты — в px до SS."""
    k = size / 800  # всё размечено для карточки 800×800 и масштабируется
    days = (target - today).days
    frac = remaining_fraction(created, target, today)
    special = days == 0 or days in MILESTONES
    cx = x + size / 2

    def px(v: float) -> int:
        return int(v * SS)

    # Заголовок и дата
    text, font = _fit(draw, title, "SemiBold", int(44 * k), int(size * 0.86))
    draw.text((px(cx), px(y + 95 * k)), text, font=font, fill=t.text, anchor="mm")
    draw.text(
        (px(cx), px(y + 145 * k)), f"до {ru_date(target)}",
        font=_font("Regular", int(26 * k)), fill=t.muted, anchor="mm",
    )

    # Кольцо
    cy, r, w = y + 430 * k, 250 * k, 26 * k
    box = [px(cx - r), px(cy - r), px(cx + r), px(cy + r)]
    draw.arc(box, 0, 360, fill=t.track, width=px(w))
    ring = t.accent if special else t.ring
    if days == 0:
        draw.arc(box, 0, 360, fill=t.accent, width=px(w))
    elif frac > 0:
        draw.arc(box, -90, -90 + 360 * frac, fill=ring, width=px(w))
        # закруглённые концы дуги
        for ang in (-90, -90 + 360 * frac):
            a = math.radians(ang)
            ex, ey = cx + (r - w / 2) * math.cos(a), cy + (r - w / 2) * math.sin(a)
            draw.ellipse([px(ex - w / 2), px(ey - w / 2), px(ex + w / 2), px(ey + w / 2)], fill=ring)

    # Центр
    if days == 0:
        draw.text((px(cx), px(cy - 10 * k)), "Сегодня", font=_font("Bold", int(80 * k)), fill=t.text, anchor="mm")
        draw.text((px(cx), px(cy + 75 * k)), "этот день настал", font=_font("Regular", int(32 * k)), fill=t.muted, anchor="mm")
    else:
        num = str(days)
        num_size = 150 if len(num) <= 3 else 120
        draw.text((px(cx), px(cy - 20 * k)), num, font=_font("Bold", int(num_size * k)), fill=t.text, anchor="mm")
        draw.text((px(cx), px(cy + 85 * k)), plural_days(days), font=_font("Regular", int(34 * k)), fill=t.muted, anchor="mm")

    # Низ: особая подпись или диапазон дат
    if days in MILESTONES:
        draw.text((px(cx), px(y + 740 * k)), MILESTONES[days], font=_font("SemiBold", int(26 * k)), fill=t.accent, anchor="mm")
    elif footer and days > 0:
        draw.text(
            (px(cx), px(y + 740 * k)),
            f"{ru_date(created)}  →  {ru_date(target)}   ·   осталось {round(frac * 100)}%",
            font=_font("Regular", int(22 * k)), fill=t.faint, anchor="mm",
        )


def _png(img: Image.Image, size: tuple[int, int]) -> bytes:
    buf = io.BytesIO()
    img.resize(size, Image.LANCZOS).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_card(title: str, created: date, target: date, today: date, theme: str = "dark") -> bytes:
    """Большая карточка одного события."""
    t = THEMES.get(theme, THEMES["dark"])
    W = 800
    img = Image.new("RGB", (W * SS, W * SS), t.bg)
    _draw_card(ImageDraw.Draw(img), 0, 0, W, title, created, target, today, t, footer=True)
    return _png(img, (W, W))


GRID_MAX = 12


def render_grid(events: list[tuple[str, date, date]], today: date, theme: str = "dark") -> bytes:
    """Ежедневная сводка: одно событие — большая карточка, несколько — сетка колец."""
    if len(events) == 1:
        title, created, target = events[0]
        return render_card(title, created, target, today, theme)
    t = THEMES.get(theme, THEMES["dark"])
    shown = events[:GRID_MAX]
    cols = 2 if len(shown) <= 4 else 3
    rows = math.ceil(len(shown) / cols)
    cell, gap, pad = 400, 0, 20
    W = pad * 2 + cols * cell
    H = pad * 2 + rows * cell + (50 if len(events) > GRID_MAX else 0)
    img = Image.new("RGB", (W * SS, H * SS), t.bg)
    draw = ImageDraw.Draw(img)
    for i, (title, created, target) in enumerate(shown):
        x = pad + (i % cols) * (cell + gap)
        y = pad + (i // cols) * (cell + gap)
        _draw_card(draw, x, y, cell, title, created, target, today, t, footer=False)
    if len(events) > GRID_MAX:
        rest = len(events) - GRID_MAX
        draw.text(
            (W * SS // 2, (H - 35) * SS), f"и ещё {rest} — в «Мои даты»",
            font=_font("Regular", 22), fill=t.muted, anchor="mm",
        )
    return _png(img, (W, H))


def render_avatar() -> bytes:
    """Аватарка бота в стиле карточек."""
    t = THEMES["dark"]
    W = 640
    img = Image.new("RGB", (W * SS, W * SS), t.bg)
    d = ImageDraw.Draw(img)
    c, r, w = W / 2, 200, 44
    box = [int((c - r) * SS), int((c - r) * SS), int((c + r) * SS), int((c + r) * SS)]
    d.arc(box, 0, 360, fill=t.track, width=int(w * SS))
    d.arc(box, -90, 180, fill=t.ring, width=int(w * SS))
    for ang in (-90, 180):
        a = math.radians(ang)
        ex, ey = c + (r - w / 2) * math.cos(a), c + (r - w / 2) * math.sin(a)
        d.ellipse([int((ex - w / 2) * SS), int((ey - w / 2) * SS), int((ex + w / 2) * SS), int((ey + w / 2) * SS)], fill=t.ring)
    d.ellipse([int((c - 14) * SS), int((c - 14) * SS), int((c + 14) * SS), int((c + 14) * SS)], fill=t.accent)
    return _png(img, (W, W))
