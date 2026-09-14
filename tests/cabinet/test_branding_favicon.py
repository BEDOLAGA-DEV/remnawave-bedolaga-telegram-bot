"""GET /cabinet/branding/favicon — иконка вкладки, которую понимает Safari.

Safari берёт фавикон только при первой загрузке страницы и игнорирует смену
через JS, поэтому кабинет ссылается на этот адрес прямо из index.html.
Эндпоинт обязан ответить картинкой всегда: логотип из админки, а без него —
монограмма первой буквы имени, та же, что рисует кабинет.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.responses import FileResponse
from PIL import Image
from structlog.testing import capture_logs

from app.cabinet.routes import branding as branding_routes
from app.cabinet.utils import brand_monogram
from app.cabinet.utils.brand_monogram import MONOGRAM_PNG_SIZE, monogram_png, monogram_svg
from app.cabinet.utils.favicon_tile import FAVICON_CORNER_RATIO, FAVICON_TILE_SIZE
from app.config import settings


def _name(value: str | None) -> AsyncMock:
    return AsyncMock(return_value=value)


async def test_without_logo_returns_png_monogram_of_the_first_letter(monkeypatch) -> None:
    # Именно PNG: SVG-фавикон Safari рисует монохромной плиткой с буквой, теряя цвета.
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: None)

    with patch('app.cabinet.routes.branding.get_setting_value', _name('zeroping')):
        response = await branding_routes.get_favicon(db=AsyncMock())

    assert response.media_type == 'image/png'
    assert response.body == monogram_png('Z')
    assert response.headers['cache-control'] == 'public, max-age=300'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['vary'] == 'Origin'


async def test_empty_name_falls_back_to_v(monkeypatch) -> None:
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: None)

    with patch('app.cabinet.routes.branding.get_setting_value', _name('')):
        response = await branding_routes.get_favicon(db=AsyncMock())

    assert response.body == monogram_png('V')


async def test_unset_name_uses_build_default(monkeypatch) -> None:
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: None)
    # У Settings нет поля CABINET_BRANDING_NAME — маршрут читает его через getattr с None.
    assert getattr(settings, 'CABINET_BRANDING_NAME', None) is None
    monkeypatch.delenv('VITE_APP_NAME', raising=False)

    with patch('app.cabinet.routes.branding.get_setting_value', _name(None)):
        response = await branding_routes.get_favicon(db=AsyncMock())

    assert response.body == monogram_png('C')  # «Cabinet»


async def test_render_failure_falls_back_to_svg(monkeypatch) -> None:
    # Pillow не смог отрисовать (нет шрифта, битая буква) — отдаём SVG, а не 500,
    # и оставляем след в логе: молча деградировать нельзя.
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: None)

    def broken(letter: str | None) -> bytes:
        raise RuntimeError('no font')

    monkeypatch.setattr(brand_monogram, 'monogram_png', broken)
    with (
        patch('app.cabinet.routes.branding.get_setting_value', _name('zeroping')),
        capture_logs() as logs,
    ):
        response = await branding_routes.get_favicon(db=AsyncMock())

    assert response.media_type == 'image/svg+xml'
    assert response.body.decode() == monogram_svg('Z')
    assert any(log['log_level'] == 'warning' and 'монограмм' in log['event'] for log in logs)


def test_monogram_png_is_a_square_raster_with_the_letter_drawn() -> None:
    png = monogram_png('Z')
    assert png.startswith(b'\x89PNG\r\n\x1a\n')
    image = Image.open(BytesIO(png))
    assert image.size == (MONOGRAM_PNG_SIZE, MONOGRAM_PNG_SIZE)
    # Буква действительно нарисована: разные буквы дают разные картинки,
    # а пустое имя — ту же монограмму, что «V».
    letters = {key: monogram_png(key) for key in ('Z', 'V', '   ', 'я', 'Я', 'Ж')}
    assert letters['Z'] != letters['V']
    assert letters['   '] == letters['V']
    assert letters['я'] == letters['Я']
    # Кириллица есть в шрифте: у встроенного шрифта Pillow её нет, и любая
    # русская буква рисовалась бы одним и тем же «квадратом» вместо буквы.
    assert letters['Я'] != letters['Ж']
    # Кеш: один и тот же объект байтов на повторный вызов.
    again = monogram_png('Z')
    assert again is letters['Z']


def _write_logo(path: Path, size: int = 40, color: str = '#2ee6a6') -> None:
    Image.new('RGBA', (size, size), color).save(path)


def test_corner_ratio_stays_below_the_safari_plate_threshold() -> None:
    # Safari в тёмной теме подрисовывает иконке с прозрачными углами белую
    # плитку-подложку, если скругление заметное: при 0,16 стороны и больше
    # подложка есть, при 0,12 — нет (Safari 26.6, замерено на живых вкладках).
    # Эту иконку видит только Safari, поэтому радиус меньше, чем у плитки в
    # шапке кабинета (0,3), и поднимать его «для красоты» нельзя.
    assert 0 < FAVICON_CORNER_RATIO <= 0.12


async def test_with_logo_serves_a_rounded_tile_with_short_cache(tmp_path: Path, monkeypatch) -> None:
    # Safari берёт иконку только по этой ссылке и смену через JS не видит, а
    # скруглённую плитку из логотипа кабинет рисует уже на canvas — Safari
    # оставался с квадратом. Скругляем здесь.
    logo = tmp_path / 'logo.png'
    _write_logo(logo)
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: logo)

    response = await branding_routes.get_favicon(db=AsyncMock())

    assert response.media_type == 'image/png'
    assert response.headers['cache-control'] == 'public, max-age=300'
    assert response.headers['vary'] == 'Origin'
    image = Image.open(BytesIO(response.body)).convert('RGBA')
    assert image.size == (FAVICON_TILE_SIZE, FAVICON_TILE_SIZE)
    assert image.getpixel((0, 0))[3] == 0, 'угол прозрачный'
    assert image.getpixel((FAVICON_TILE_SIZE - 1, FAVICON_TILE_SIZE - 1))[3] == 0
    center = image.getpixel((FAVICON_TILE_SIZE // 2, FAVICON_TILE_SIZE // 2))
    assert center[3] == 255 and center[:3] == (0x2E, 0xE6, 0xA6), 'центр — сам логотип'


async def test_rounded_tile_is_cached_until_the_logo_file_changes(tmp_path: Path, monkeypatch) -> None:
    logo = tmp_path / 'logo.png'
    _write_logo(logo)
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: logo)

    first = await branding_routes.get_favicon(db=AsyncMock())
    second = await branding_routes.get_favicon(db=AsyncMock())
    assert first.body is second.body

    _write_logo(logo, color='#ff0000')
    # Новая загрузка в админке — новый файл с новым mtime; подстрахуемся от той же секунды.
    import os

    os.utime(logo, ns=(logo.stat().st_atime_ns, logo.stat().st_mtime_ns + 1_000_000))
    third = await branding_routes.get_favicon(db=AsyncMock())
    assert third.body != first.body
    center = Image.open(BytesIO(third.body)).convert('RGBA').getpixel((128, 128))
    assert center[:3] == (255, 0, 0)


async def test_svg_logo_is_served_as_is(tmp_path: Path, monkeypatch) -> None:
    # SVG Pillow не растеризует — отдаём файл как есть (лучше, чем ничего).
    logo = tmp_path / 'logo.svg'
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: logo)

    response = await branding_routes.get_favicon(db=AsyncMock())

    assert isinstance(response, FileResponse)
    assert Path(response.path) == logo
    assert response.media_type == 'image/svg+xml'
    assert response.headers['cache-control'] == 'public, max-age=300'


async def test_unreadable_logo_falls_back_to_the_raw_file(tmp_path: Path, monkeypatch) -> None:
    logo = tmp_path / 'logo.png'
    logo.write_bytes(b'\x89PNG\r\n\x1a\n')  # обрезанный файл
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: logo)

    with capture_logs() as logs:
        response = await branding_routes.get_favicon(db=AsyncMock())

    assert isinstance(response, FileResponse)
    assert Path(response.path) == logo
    assert response.media_type == 'image/png'
    assert any(log['log_level'] == 'warning' and 'плитк' in log['event'] for log in logs)


async def test_logo_endpoint_keeps_its_hour_cache(tmp_path: Path, monkeypatch) -> None:
    logo = tmp_path / 'logo.svg'
    logo.write_text('<svg/>')
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: logo)

    response = await branding_routes.get_logo()

    assert response.media_type == 'image/svg+xml'
    assert response.headers['cache-control'] == 'public, max-age=3600'
    assert 'sandbox' in response.headers['content-security-policy']
    # Логотип грузится и как <img>/фавикон (без Origin), и fetch()-ом (с Origin):
    # без Vary кеш отдал бы fetch()-у ответ без CORS-заголовков.
    assert response.headers['vary'] == 'Origin'


def test_monogram_escapes_and_uppercases() -> None:
    assert '>Z</text>' in monogram_svg('zeroping')
    assert '>Я</text>' in monogram_svg('я')
    assert '>&amp;</text>' in monogram_svg('&')
    assert '>V</text>' in monogram_svg('   ')
    assert monogram_svg('Z').startswith(
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
    )
