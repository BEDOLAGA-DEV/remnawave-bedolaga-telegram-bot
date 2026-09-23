"""Иконки установленного приложения (web app manifest) кабинета.

Chrome на Android собирает из манифеста WebAPK на серверах Google, и те скачивают
манифест и иконки по их адресам. Иконки в data: URI (как их рисует кабинет на
canvas) этим серверам недоступны — установка молча откатывается к ярлыку во
вкладке браузера. Поэтому PNG для манифеста отдаёт бот по обычным URL.

Геометрия та же, что у ярлыков кабинета (squareIconDataUri в src/utils/favicon.ts):
непрозрачный квадрат без скругления — маску накладывает сама система, а
прозрачные углы Android рисует белым. Логотип вписан целиком (contain) на фоне
темы; maskable-вариант держит содержимое в безопасной зоне Android — центральных
80 % стороны. Без логотипа — монограмма первой буквы на цвете акцента.
"""

from __future__ import annotations

import re
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from app.utils.logo_fingerprint import logo_fingerprint as _logo_fingerprint

from .brand_monogram import bold_font, monogram_letter
from .favicon_tile import is_raster_logo


APP_ICON_SIZES = (192, 512)
# Ревизия отрисовки: входит в ``?v=`` адресов иконок. Поднимать, когда при тех же
# логотипе и цветах меняется сама картинка, — иначе уже установленные приложения
# останутся со старой иконкой (Chrome перекачивает её только по новому адресу).
RENDER_REVISION = 2
# Безопасная зона maskable-иконок Android — как MASKABLE_SAFE_ZONE в кабинете.
MASKABLE_SAFE_ZONE = 0.8
# Кегль буквы монограммы относительно стороны — как в SVG кабинета (38 из 64).
_MONOGRAM_FONT_RATIO = 38 / 64

_HEX_COLOR = re.compile(r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$')
_WHITE = (255, 255, 255)
# Тёмный текст на светлом акценте — тот же, что readableTextOnHex в кабинете.
_INK = (15, 23, 42)
_WHITE_TEXT_MIN_CONTRAST = 4.5
# Логотип «залит до краёв»: доля непрозрачных пикселей каймы, доля сошедшихся к
# одному цвету и допуск на канал (сглаживание и JPEG-шум дают разброс в единицы).
_EDGE_OPAQUE_ALPHA = 250
_EDGE_MIN_OPAQUE_SHARE = 0.5
_EDGE_MIN_UNIFORM_SHARE = 0.9
_EDGE_COLOR_TOLERANCE = 12


def is_hex_color(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX_COLOR.match(value))


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    digits = value[1:]
    if len(digits) == 3:
        digits = ''.join(ch * 2 for ch in digits)
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def readable_text_on(background: str) -> str:
    """Белый или тёмный текст на заливке — тот же выбор, что readableTextOnHex в кабинете."""
    bg = _hex_to_rgb(background)
    white = _contrast(_WHITE, bg)
    if white >= _WHITE_TEXT_MIN_CONTRAST or white >= _contrast(_INK, bg):
        return '#ffffff'
    return '#0f172a'


def _to_png(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format='PNG', optimize=True)
    return buffer.getvalue()


def logo_edge_color(logo: Image.Image) -> tuple[int, int, int] | None:
    """Цвет, которым логотип залит до краёв; ``None`` — единого цвета у краёв нет.

    Логотип-плитка (сплошной квадрат, в том числе с запечённым скруглением) на фоне
    темы превращается в квадрат в рамке: maskable-вариант ужимает содержимое до
    80 %, и на рабочем столе Android у иконки видны «чёрные полоски по краям».
    Такой логотип сам задаёт фон — продолжаем его цвет на всю плитку.

    Смотрим внешнюю кайму в один пиксель. Прозрачные пиксели не считаются (углы
    запечённого скругления), но непрозрачных должно быть большинство: у знака на
    прозрачном фоне кайма пустая, и фоном остаётся тема. Непрозрачные обязаны
    сходиться к одному цвету — у фото и градиентов его нет, угадывать нельзя.
    """
    rgba = logo.convert('RGBA')
    width, height = rgba.size
    if width < 2 or height < 2:
        return None
    pixels = rgba.load()
    ring = [(x, y) for x in range(width) for y in (0, height - 1)]
    ring += [(x, y) for y in range(1, height - 1) for x in (0, width - 1)]
    opaque = [pixels[x, y][:3] for x, y in ring if pixels[x, y][3] >= _EDGE_OPAQUE_ALPHA]
    if len(opaque) < len(ring) * _EDGE_MIN_OPAQUE_SHARE:
        return None
    median = tuple(sorted(color[channel] for color in opaque)[len(opaque) // 2] for channel in range(3))
    matching = sum(
        1
        for color in opaque
        if all(abs(color[channel] - median[channel]) <= _EDGE_COLOR_TOLERANCE for channel in range(3))
    )
    if matching < len(opaque) * _EDGE_MIN_UNIFORM_SHARE:
        return None
    return median


def logo_app_icon(logo_path: Path, size: int, background: str, content_scale: float = 1.0) -> bytes:
    """PNG ``size``×``size``: логотип вписан целиком по центру на непрозрачном фоне.

    Фон — цвет краёв логотипа, если он залит до краёв (см. ``logo_edge_color``),
    иначе ``background``.
    """
    with Image.open(logo_path) as source:
        logo = source.convert('RGBA')
    fill = logo_edge_color(logo) or background
    canvas = Image.new('RGB', (size, size), fill)
    # Сводим с фоном ДО уменьшения: ресайз полупрозрачных пикселей подмешивает к ним
    # цвет прозрачных (чёрный), и вокруг логотипа остаётся тёмный шов.
    flattened = Image.new('RGBA', logo.size, fill)
    flattened.alpha_composite(logo)
    logo = flattened
    scale = min(size / logo.width, size / logo.height) * content_scale
    width, height = max(1, round(logo.width * scale)), max(1, round(logo.height * scale))
    logo = logo.resize((width, height), Image.Resampling.LANCZOS)
    canvas.paste(logo, ((size - width) // 2, (size - height) // 2), logo)
    return _to_png(canvas)


def monogram_app_icon(letter: str | None, size: int, background: str, content_scale: float = 1.0) -> bytes:
    """PNG ``size``×``size``: первая буква на сплошной заливке ``background``."""
    image = Image.new('RGB', (size, size), background)
    center = size / 2
    ImageDraw.Draw(image).text(
        (center, center),
        monogram_letter(letter),
        fill=readable_text_on(background),
        font=bold_font(round(size * _MONOGRAM_FONT_RATIO * content_scale)),
        anchor='mm',
    )
    return _to_png(image)


@lru_cache(maxsize=16)
def _cached_logo_icon(
    logo_path: str, mtime_ns: int, file_size: int, size: int, background: str, content_scale: float
) -> bytes:
    # mtime и размер в ключе: новый логотип из админки — новый файл, старые иконки забываются.
    return logo_app_icon(Path(logo_path), size, background, content_scale)


@lru_cache(maxsize=16)
def _cached_monogram_icon(letter: str, size: int, background: str, content_scale: float) -> bytes:
    return monogram_app_icon(letter, size, background, content_scale)


def render_app_icon(
    *,
    logo_path: Path | None,
    letter: str | None,
    size: int,
    maskable: bool,
    background: str,
    accent: str,
) -> bytes:
    """Иконка приложения: из логотипа, если он растровый и читается, иначе монограмма.

    Исключения Pillow по логотипу пробрасываются — вызывающий решает, чем их заменить.
    """
    content_scale = MASKABLE_SAFE_ZONE if maskable else 1.0
    if logo_path is not None and is_raster_logo(logo_path):
        stat = logo_path.stat()
        return _cached_logo_icon(str(logo_path), stat.st_mtime_ns, stat.st_size, size, background, content_scale)
    return _cached_monogram_icon(monogram_letter(letter), size, accent, content_scale)


# Отпечаток общий с логотипом бота (адрес rich-меню и кэш file_id) — одна реализация.
logo_fingerprint = _logo_fingerprint
