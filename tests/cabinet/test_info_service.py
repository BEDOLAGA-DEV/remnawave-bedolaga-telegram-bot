"""GET /cabinet/info/service отдаёт настройки инстанса, а не заглушку.

Ручка собирала ответ из SERVICE_NAME / SERVICE_DESCRIPTION / BOT_NAME /
WEBSITE_URL / SUPPORT_TELEGRAM через ``getattr(settings, ..., None)``. Ни одного
такого поля в ``Settings`` нет, а ``extra='ignore'`` не даёт задать их и через
.env — любой инстанс отвечал одинаковым «VPN Service» с пустыми контактами.

Поэтому тесты пиняют две вещи: читаются существующие поля (иначе тихий откат к
дефолту повторится) и незаполненный контакт остаётся ``null``.
"""

import pytest

from app.cabinet.routes.info import get_service_info
from app.config import Settings, settings


BRANDING_KEYS = (
    'MINIAPP_SERVICE_NAME_RU',
    'MINIAPP_SERVICE_NAME_EN',
    'MINIAPP_SERVICE_DESCRIPTION_RU',
    'MINIAPP_SERVICE_DESCRIPTION_EN',
)


@pytest.fixture
def branded(monkeypatch):
    monkeypatch.setattr(settings, 'MINIAPP_SERVICE_NAME_RU', 'Ромашка VPN')
    monkeypatch.setattr(settings, 'MINIAPP_SERVICE_NAME_EN', 'Camomile VPN')
    monkeypatch.setattr(settings, 'MINIAPP_SERVICE_DESCRIPTION_RU', 'Быстро и надёжно')
    monkeypatch.setattr(settings, 'MINIAPP_SERVICE_DESCRIPTION_EN', 'Fast and reliable')


@pytest.mark.parametrize(
    ('field', 'value'),
    [('SUPPORT_EMAIL', 'help@example.com'), ('SERVICE_WEBSITE_URL', 'https://example.com')],
)
def test_contact_settings_exist(field: str, value: str) -> None:
    """Поля должны быть в модели, иначе .env их не задаст, а ручка снова врёт.

    Именно на этом ручка и горела: ``getattr`` молча отдавал дефолт, тесты
    зелёные, а эндпоинт годами возвращал одно и то же на всех инстансах.
    """
    assert field in Settings.model_fields
    assert getattr(Settings(BOT_TOKEN='1:test', **{field: value}), field) == value


async def test_name_and_description_come_from_branding(branded):
    """Источник тот же, что у мини-аппа — сервис не называется в двух местах по-разному."""
    ru = await get_service_info(language='ru')
    en = await get_service_info(language='en')

    assert (ru.name, ru.description) == ('Ромашка VPN', 'Быстро и надёжно')
    assert (en.name, en.description) == ('Camomile VPN', 'Fast and reliable')


@pytest.mark.parametrize('language', ['fa', 'zh', 'ru-RU', '  ru'])
async def test_unknown_and_dirty_language_codes_resolve(branded, language: str):
    """Локали без брендинга берут дефолт, а хвост региона/пробелы не мешают."""
    response = await get_service_info(language=language)

    expected = 'Ромашка VPN' if language.strip().startswith('ru') else 'Camomile VPN'
    assert response.name == expected


async def test_contacts_are_returned(monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_EMAIL', 'help@example.com')
    monkeypatch.setattr(settings, 'SUPPORT_USERNAME', '@camomile_support')
    monkeypatch.setattr(settings, 'SERVICE_WEBSITE_URL', 'https://example.com')

    response = await get_service_info(language='ru')

    assert response.support_email == 'help@example.com'
    assert response.support_telegram == '@camomile_support'
    assert response.website == 'https://example.com'


@pytest.mark.parametrize('blank', ['', '   '], ids=['empty', 'whitespace'])
async def test_blank_contacts_are_null_not_empty_string(monkeypatch, blank: str):
    """Пустая переменная в .env — это «контакта нет», как и до правки."""
    monkeypatch.setattr(settings, 'SUPPORT_EMAIL', blank)
    monkeypatch.setattr(settings, 'SUPPORT_USERNAME', blank)
    monkeypatch.setattr(settings, 'SERVICE_WEBSITE_URL', blank)

    response = await get_service_info(language='ru')

    assert response.support_email is None
    assert response.support_telegram is None
    assert response.website is None


async def test_name_is_never_the_old_hardcoded_stub(monkeypatch):
    """Даже с пустым брендингом имя берётся из фолбэка брендинга, а не из ручки."""
    for key in BRANDING_KEYS:
        monkeypatch.setattr(settings, key, '')

    response = await get_service_info(language='ru')

    assert response.name == settings.get_miniapp_branding()['service_name']['default']
    assert response.name != 'VPN Service'
    assert response.description
