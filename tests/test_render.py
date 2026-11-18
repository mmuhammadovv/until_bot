from datetime import date

from PIL import Image
import io

from until_bot.render import remaining_fraction, render_avatar, render_card, render_grid

T = date(2026, 11, 18)


def size(png):
    return Image.open(io.BytesIO(png)).size


def test_fraction():
    assert remaining_fraction(date(2026, 11, 18), date(2027, 11, 18), T) == 1.0
    assert remaining_fraction(date(2026, 1, 1), date(2026, 11, 18), T) == 0.0
    assert 0.4 < remaining_fraction(date(2026, 10, 1), date(2026, 12, 31), T) < 0.5


def test_render_all_kinds():
    for theme in ("dark", "light"):
        assert size(render_card("Новый год", date(2026, 10, 1), date(2026, 12, 31), T, theme)) == (800, 800)
        assert size(render_card("Сегодня", T, T, T, theme)) == (800, 800)
        assert size(render_card("Очень " * 30, T, date(2030, 1, 1), T, theme)) == (800, 800)
    ev = [(f"E{i}", T, date(2027, 1, i + 1)) for i in range(15)]
    assert size(render_grid(ev[:1], T)) == (800, 800)
    assert size(render_grid(ev[:3], T))[0] == 840
    assert size(render_grid(ev, T))[0] == 1240
    assert size(render_avatar()) == (640, 640)
