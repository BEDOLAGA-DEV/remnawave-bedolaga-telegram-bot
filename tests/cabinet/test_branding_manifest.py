"""GET /cabinet/branding/manifest.webmanifest и /cabinet/branding/app-icon/… .

Chrome на Android собирает установленное приложение (WebAPK) на серверах Google,
и те скачивают манифест и иконки по их адресам. Манифест в data: URI, который
строит кабинет, им недоступен — вместо приложения ставится ярлык во вкладке.
Поэтому манифест и иконки отдаёт бот по обычным URL, из настроек брендинга.
"""

from __future__ import annotations

import json
import os
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urljoin

import pytest
from fastapi import HTTPException
from PIL import Image
from structlog.testing import capture_logs

from app.cabinet.routes import branding as branding_routes
from app.cabinet.utils import app_icon


MANIFEST_URL = 'https://cabinet.example.com/api/cabinet/branding/manifest.webmanifest'


def _settings(values: dict[str, str | None]) -> AsyncMock:
    return AsyncMock(side_effect=lambda db, key: values.get(key))


def _write_logo(path: Path, size: tuple[int, int] = (40, 40), color: str = '#2ee6a6') -> None:
    Image.new('RGBA', size, color).save(path)


def _write_glyph_logo(path: Path, size: tuple[int, int], color: str = '#2ee6a6') -> None:
    """Знак на прозрачном фоне: по краям прозрачная кайма, цвет — только внутри."""
    logo = Image.new('RGBA', size, (0, 0, 0, 0))
    width, height = size
    margin = max(1, min(width, height) // 20)
    logo.paste(Image.new('RGBA', (width - 2 * margin, height - 2 * margin), color), (margin, margin))
    logo.save(path)


async def _manifest(values: dict[str, str | None], base: str = '/') -> dict:
    with patch('app.cabinet.routes.branding.get_setting_value', _settings(values)):
        response = await branding_routes.get_web_manifest(base=base, db=AsyncMock())
    assert response.media_type == 'application/manifest+json'
    assert response.headers['cache-control'] == 'public, max-age=300'
    assert response.headers['x-content-type-options'] == 'nosniff'
    return json.loads(response.body)


async def _icon(values: dict[str, str | None], size: int, *, maskable: bool = False) -> Image.Image:
    route = branding_routes.get_maskable_app_icon if maskable else branding_routes.get_app_icon
    with patch('app.cabinet.routes.branding.get_setting_value', _settings(values)):
        response = await route(size=size, db=AsyncMock())
    assert response.media_type == 'image/png'
    assert response.headers['cache-control'] == 'public, max-age=300'
    return Image.open(BytesIO(response.body))


async def test_manifest_uses_branding_name_and_theme_colors(monkeypatch) -> None:
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: None)
    values = {
        branding_routes.BRANDING_NAME_KEY: '  ZeroPing  ',
        branding_routes.THEME_COLORS_KEY: json.dumps({'darkBackground': '#101820', 'accent': '#ff5500'}),
    }

    manifest = await _manifest(values)

    assert manifest['name'] == 'ZeroPing'
    assert manifest['short_name'] == 'ZeroPing'
    assert manifest['display'] == 'standalone'
    assert manifest['start_url'] == '/'
    assert manifest['scope'] == '/'
    assert manifest['background_color'] == '#101820'
    assert manifest['theme_color'] == '#101820'


async def test_manifest_icons_are_fetchable_urls_not_data_uris(monkeypatch) -> None:
    # Весь смысл эндпоинта: у каждой иконки настоящий адрес, который сервер WebAPK скачает.
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: None)

    manifest = await _manifest({})

    icons = manifest['icons']
    assert {(icon['sizes'], icon['purpose']) for icon in icons} == {
        ('192x192', 'any'),
        ('512x512', 'any'),
        ('192x192', 'maskable'),
        ('512x512', 'maskable'),
    }
    for icon in icons:
        assert icon['type'] == 'image/png'
        assert not icon['src'].startswith(('data:', '/', 'http')), 'относительный адрес'
    # Относительный адрес разрешается от адреса манифеста — при любом префиксе API.
    resolved = {urljoin(MANIFEST_URL, icon['src']).split('?')[0] for icon in icons}
    assert resolved == {
        'https://cabinet.example.com/api/cabinet/branding/app-icon/192.png',
        'https://cabinet.example.com/api/cabinet/branding/app-icon/512.png',
        'https://cabinet.example.com/api/cabinet/branding/app-icon/maskable/192.png',
        'https://cabinet.example.com/api/cabinet/branding/app-icon/maskable/512.png',
    }


async def test_light_background_when_dark_theme_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: None)
    values = {
        branding_routes.ENABLED_THEMES_KEY: json.dumps({'dark': False, 'light': True}),
        branding_routes.THEME_COLORS_KEY: json.dumps({'lightBackground': '#fafafa'}),
    }

    manifest = await _manifest(values)

    assert manifest['background_color'] == '#fafafa'


async def test_broken_stored_colors_fall_back_to_defaults(monkeypatch) -> None:
    # В манифест не должно попасть ничего, кроме цвета: иначе Chrome отвергнет его целиком.
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: None)
    values = {
        branding_routes.THEME_COLORS_KEY: json.dumps({'darkBackground': 'red; x', 'accent': 42}),
        branding_routes.ENABLED_THEMES_KEY: 'not json',
    }

    manifest = await _manifest(values)

    assert manifest['background_color'] == branding_routes.DEFAULT_THEME_COLORS['darkBackground']


async def test_empty_name_uses_build_default(monkeypatch) -> None:
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: None)
    monkeypatch.delenv('VITE_APP_NAME', raising=False)

    manifest = await _manifest({branding_routes.BRANDING_NAME_KEY: '   '})

    assert manifest['name'] == 'Cabinet'


@pytest.mark.parametrize(
    ('base', 'expected'),
    [
        ('/', '/'),
        ('/cabinet/', '/cabinet/'),
        ('/cabinet', '/cabinet/'),
        ('https://evil.example/', '/'),
        ('//evil.example/', '/'),
        ('/\\evil.example/', '/'),
        ('/ca binet/', '/'),
        ('', '/'),
        ('/' + 'a' * 300, '/'),
    ],
)
async def test_start_url_accepts_only_a_same_site_path(monkeypatch, base: str, expected: str) -> None:
    # Чужой адрес в start_url увёл бы установленное приложение на другой сайт.
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: None)

    manifest = await _manifest({}, base=base)

    assert manifest['start_url'] == expected
    assert manifest['scope'] == expected


async def test_icon_version_changes_with_logo_name_and_colors(tmp_path: Path, monkeypatch) -> None:
    # Новый ?v= — новый адрес иконки: Chrome обновит иконку установленного приложения.
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: None)

    def version(manifest: dict) -> str:
        versions = {icon['src'].split('?v=')[1] for icon in manifest['icons']}
        assert len(versions) == 1
        return versions.pop()

    base_version = version(await _manifest({branding_routes.BRANDING_NAME_KEY: 'Alpha'}))
    assert version(await _manifest({branding_routes.BRANDING_NAME_KEY: 'Alpha'})) == base_version
    assert version(await _manifest({branding_routes.BRANDING_NAME_KEY: 'Beta'})) != base_version
    recolored = {
        branding_routes.BRANDING_NAME_KEY: 'Alpha',
        branding_routes.THEME_COLORS_KEY: json.dumps({'accent': '#ff0000'}),
    }
    assert version(await _manifest(recolored)) != base_version

    logo = tmp_path / 'logo.png'
    _write_logo(logo)
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: logo)
    with_logo = version(await _manifest({branding_routes.BRANDING_NAME_KEY: 'Alpha'}))
    assert with_logo != base_version

    _write_logo(logo, color='#ff0000')
    os.utime(logo, ns=(logo.stat().st_atime_ns, logo.stat().st_mtime_ns + 1_000_000))
    assert version(await _manifest({branding_routes.BRANDING_NAME_KEY: 'Alpha'})) != with_logo


@pytest.mark.parametrize('size', app_icon.APP_ICON_SIZES)
@pytest.mark.parametrize('maskable', [False, True])
async def test_logo_icon_is_an_opaque_square_of_the_exact_size(
    tmp_path: Path, monkeypatch, size: int, maskable: bool
) -> None:
    # Прозрачные углы Android рисует белым — иконка непрозрачная, на фоне темы.
    logo = tmp_path / 'logo.png'
    _write_glyph_logo(logo, size=(200, 100))
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: logo)
    values = {branding_routes.THEME_COLORS_KEY: json.dumps({'darkBackground': '#102030'})}

    image = (await _icon(values, size, maskable=maskable)).convert('RGBA')

    assert image.size == (size, size)
    assert image.getpixel((0, 0)) == (0x10, 0x20, 0x30, 255), 'угол — фон темы'
    assert image.getpixel((size // 2, size // 2)) == (0x2E, 0xE6, 0xA6, 255), 'центр — логотип'
    # Широкий логотип вписан целиком (contain): у левого края по центру — логотип,
    # а у maskable он отступает к безопасной зоне и край остаётся фоном.
    edge = image.getpixel((size // 10, size // 2))
    if maskable:
        assert edge == (0x10, 0x20, 0x30, 255)
    else:
        assert edge == (0x2E, 0xE6, 0xA6, 255)


@pytest.mark.parametrize('size', app_icon.APP_ICON_SIZES)
@pytest.mark.parametrize('maskable', [False, True])
async def test_full_bleed_logo_fills_the_whole_tile_with_its_own_color(
    tmp_path: Path, monkeypatch, size: int, maskable: bool
) -> None:
    """Логотип — сплошная плитка (красный квадрат). На фоне тёмной темы maskable-вариант
    (содержимое в 80 %) выходил красным квадратом в чёрной рамке — на рабочем столе
    Android у иконки «чёрные полоски по краям». Фон продолжает цвет краёв логотипа."""
    logo = tmp_path / 'logo.png'
    _write_logo(logo, size=(300, 300), color='#e53935')
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: logo)
    values = {branding_routes.THEME_COLORS_KEY: json.dumps({'darkBackground': '#000000'})}

    image = (await _icon(values, size, maskable=maskable)).convert('RGB')

    red = (0xE5, 0x39, 0x35)
    for point in ((0, 0), (size - 1, 0), (1, size // 2), (size // 2, 1), (size - 1, size - 1), (size // 2, size // 2)):
        assert image.getpixel(point) == red, f'в точке {point} виден фон темы'


async def test_full_bleed_logo_with_baked_rounded_corners_gets_its_color_in_the_corners(
    tmp_path: Path, monkeypatch
) -> None:
    """Скругление, запечённое в PNG: углы прозрачные, остальной край — цвет логотипа."""
    from PIL import ImageDraw

    logo_image = Image.new('RGBA', (300, 300), (0, 0, 0, 0))
    ImageDraw.Draw(logo_image).rounded_rectangle((0, 0, 299, 299), radius=60, fill='#e53935')
    logo = tmp_path / 'logo.png'
    logo_image.save(logo)
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: logo)
    values = {branding_routes.THEME_COLORS_KEY: json.dumps({'darkBackground': '#000000'})}

    image = (await _icon(values, 512, maskable=True)).convert('RGB')

    assert image.getpixel((0, 0)) == (0xE5, 0x39, 0x35)
    assert image.getpixel((60, 60)) == (0xE5, 0x39, 0x35), 'на месте запечённого скругления — фон темы'


async def test_logo_with_a_varied_edge_keeps_the_theme_background(tmp_path: Path, monkeypatch) -> None:
    """Фото или градиент до краёв: единого цвета нет — угадывать нельзя, остаётся фон темы."""
    logo_image = Image.new('RGBA', (300, 300), '#e53935')
    logo_image.paste(Image.new('RGBA', (150, 300), '#1e88e5'), (150, 0))
    logo = tmp_path / 'logo.png'
    logo_image.save(logo)
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: logo)
    values = {branding_routes.THEME_COLORS_KEY: json.dumps({'darkBackground': '#102030'})}

    image = (await _icon(values, 512, maskable=True)).convert('RGB')

    assert image.getpixel((0, 0)) == (0x10, 0x20, 0x30)


async def test_without_logo_icon_is_a_monogram_on_the_accent(monkeypatch) -> None:
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: None)
    values = {
        branding_routes.BRANDING_NAME_KEY: 'zeroping',
        branding_routes.THEME_COLORS_KEY: json.dumps({'accent': '#1e3a8a'}),
    }

    image = (await _icon(values, 512)).convert('RGB')

    assert image.size == (512, 512)
    assert image.getpixel((0, 0)) == (0x1E, 0x3A, 0x8A)
    # Буква нарисована: на тёмном акценте в центре есть белые пиксели текста.
    assert any(image.getpixel((x, 256))[0] > 200 for x in range(200, 312))


async def test_svg_logo_falls_back_to_monogram(tmp_path: Path, monkeypatch) -> None:
    # SVG Pillow не растеризует, а манифесту нужен PNG точного размера.
    logo = tmp_path / 'logo.svg'
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: logo)

    image = await _icon({branding_routes.BRANDING_NAME_KEY: 'Z'}, 192)

    assert image.format == 'PNG'
    assert image.size == (192, 192)


async def test_unreadable_logo_falls_back_to_monogram_with_a_warning(tmp_path: Path, monkeypatch) -> None:
    logo = tmp_path / 'logo.png'
    logo.write_bytes(b'\x89PNG\r\n\x1a\n')  # обрезанный файл
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: logo)

    with capture_logs() as logs:
        image = await _icon({branding_routes.BRANDING_NAME_KEY: 'Z'}, 192)

    assert image.size == (192, 192)
    assert any(log['log_level'] == 'warning' and 'иконку приложения' in log['event'] for log in logs)


async def test_unsupported_icon_size_is_404(monkeypatch) -> None:
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: None)

    with (
        patch('app.cabinet.routes.branding.get_setting_value', _settings({})),
        pytest.raises(HTTPException) as error,
    ):
        await branding_routes.get_app_icon(size=4096, db=AsyncMock())

    assert error.value.status_code == 404


def test_readable_text_matches_the_cabinet_choice() -> None:
    assert app_icon.readable_text_on('#0a0f1a') == '#ffffff'
    assert app_icon.readable_text_on('#1e3a8a') == '#ffffff'
    # Акцент по умолчанию: белый даёт 3.68, тёмный — больше, как и в кабинете.
    assert app_icon.readable_text_on('#3b82f6') == '#0f172a'
    assert app_icon.readable_text_on('#fde047') == '#0f172a'
    assert app_icon.readable_text_on('#fff') == '#0f172a'
