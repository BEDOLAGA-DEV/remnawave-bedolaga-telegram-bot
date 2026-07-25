import pytest

from app.config import settings


TELEGRAM_CONTACT = '@help'
EXTERNAL_CONTACT = 'https://help.example.com'


async def _get_config(monkeypatch, mode: str, contact: str):
    from app.cabinet.routes.info import get_support_config

    monkeypatch.setattr(settings, 'SUPPORT_SYSTEM_MODE', mode, raising=False)
    monkeypatch.setattr(settings, 'SUPPORT_USERNAME', contact, raising=False)

    return await get_support_config()


@pytest.mark.asyncio
async def test_contact_mode_with_telegram_username(monkeypatch):
    response = await _get_config(monkeypatch, 'contact', TELEGRAM_CONTACT)

    assert response.tickets_enabled is False
    assert response.support_type == 'profile'
    assert response.support_url == 'https://t.me/help'
    assert response.support_username == '@help'
    assert response.contact_is_telegram is True


@pytest.mark.asyncio
async def test_contact_mode_with_external_url(monkeypatch):
    response = await _get_config(monkeypatch, 'contact', EXTERNAL_CONTACT)

    assert response.tickets_enabled is False
    # Раньше здесь всегда был "profile", и клиент склеивал t.me/https://help...
    assert response.support_type == 'url'
    assert response.support_url == EXTERNAL_CONTACT
    assert response.contact_is_telegram is False


@pytest.mark.asyncio
async def test_both_mode_exposes_external_url(monkeypatch):
    response = await _get_config(monkeypatch, 'both', EXTERNAL_CONTACT)

    assert response.tickets_enabled is True
    assert response.support_type == 'both'
    assert response.support_url == EXTERNAL_CONTACT
    assert response.contact_is_telegram is False


@pytest.mark.asyncio
async def test_both_mode_with_telegram_username(monkeypatch):
    response = await _get_config(monkeypatch, 'both', TELEGRAM_CONTACT)

    assert response.tickets_enabled is True
    assert response.support_type == 'both'
    assert response.support_url == 'https://t.me/help'
    assert response.contact_is_telegram is True


@pytest.mark.asyncio
@pytest.mark.parametrize('contact', [TELEGRAM_CONTACT, EXTERNAL_CONTACT])
async def test_tickets_mode_ignores_contact(monkeypatch, contact):
    response = await _get_config(monkeypatch, 'tickets', contact)

    assert response.tickets_enabled is True
    assert response.support_type == 'tickets'


@pytest.mark.asyncio
async def test_empty_contact_yields_no_url(monkeypatch):
    response = await _get_config(monkeypatch, 'both', '')

    assert response.support_url is None
    assert response.support_username is None
    assert response.contact_is_telegram is False
