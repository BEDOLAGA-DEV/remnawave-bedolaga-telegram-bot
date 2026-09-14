"""Монограмма бренда для фавикона кабинета: квадрат с первой буквой.

SVG — тот же, что рисует кабинет у себя (vite-plugins/brandMonogram.ts): цвета,
скругление, шрифт. По /cabinet/branding/favicon бот отдаёт её PNG-версию:
Safari рисует SVG-фавикон монохромной плиткой с буквой, а PNG — как есть.
SVG остаётся запасным ответом на случай, если Pillow не смог отрисовать.
"""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


MONOGRAM_BACKGROUND = '#0a0f1a'
MONOGRAM_FOREGROUND = '#ffffff'
DEFAULT_MONOGRAM_LETTER = 'V'

# Геометрия SVG (viewBox 64): плитка со скруглением 14 и буква кеглем 38.
_SVG_SIZE = 64
_SVG_CORNER_RADIUS = 14
_SVG_FONT_SIZE = 38
# PNG крупнее, чтобы вкладка и ярлыки не масштабировали вверх.
MONOGRAM_PNG_SIZE = 256
# Manrope — шрифт монограммы кабинета. Вариативный, с кириллицей; у встроенного
# шрифта Pillow кириллицы нет. Лицензия OFL — fonts/OFL.txt.
_FONT_PATH = Path(__file__).parent / 'fonts' / 'manrope-variable.ttf'
_FONT_WEIGHT = 'Bold'


def monogram_letter(letter: str | None, fallback: str = DEFAULT_MONOGRAM_LETTER) -> str:
    """Первая буква строки заглавной; при пустой строке — ``fallback``."""
    ch = (letter or '').strip()[:1].upper()
    return ch or fallback


def _escape_xml(value: str) -> str:
    return value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def monogram_svg(letter: str | None) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_SVG_SIZE}" height="{_SVG_SIZE}" '
        f'viewBox="0 0 {_SVG_SIZE} {_SVG_SIZE}">'
        f'<rect width="{_SVG_SIZE}" height="{_SVG_SIZE}" rx="{_SVG_CORNER_RADIUS}" fill="{MONOGRAM_BACKGROUND}"/>'
        f'<text x="50%" y="50%" font-family="Manrope,Arial,sans-serif" font-size="{_SVG_FONT_SIZE}" '
        f'font-weight="700" fill="{MONOGRAM_FOREGROUND}" text-anchor="middle" '
        f'dominant-baseline="central">{_escape_xml(monogram_letter(letter))}</text>'
        '</svg>'
    )


def _bold_font(size: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(_FONT_PATH), size)
    font.set_variation_by_name(_FONT_WEIGHT)
    return font


@lru_cache(maxsize=128)
def monogram_png(letter: str | None) -> bytes:
    """PNG-монограмма в геометрии SVG. Рисуется один раз на букву."""
    scale = MONOGRAM_PNG_SIZE / _SVG_SIZE
    image = Image.new('RGBA', (MONOGRAM_PNG_SIZE, MONOGRAM_PNG_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, MONOGRAM_PNG_SIZE - 1, MONOGRAM_PNG_SIZE - 1),
        radius=round(_SVG_CORNER_RADIUS * scale),
        fill=MONOGRAM_BACKGROUND,
    )
    center = MONOGRAM_PNG_SIZE / 2
    draw.text(
        (center, center),
        monogram_letter(letter),
        fill=MONOGRAM_FOREGROUND,
        font=_bold_font(round(_SVG_FONT_SIZE * scale)),
        anchor='mm',
    )
    buffer = BytesIO()
    image.save(buffer, format='PNG', optimize=True)
    return buffer.getvalue()
