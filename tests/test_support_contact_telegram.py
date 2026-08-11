import pytest

from app.config import settings


@pytest.mark.parametrize(
    'contact',
    [
        '@help',
        'help',
        't.me/help',
        'https://t.me/help',
        'https://www.t.me/help',
        'telegram.me/help',
        'https://telegram.dog/help',
        'tg://resolve?domain=help',
    ],
)
def test_telegram_contacts_detected(monkeypatch, contact):
    monkeypatch.setattr(settings, 'SUPPORT_USERNAME', contact, raising=False)
    assert settings.is_support_contact_telegram() is True


@pytest.mark.parametrize(
    'contact',
    [
        'https://help.example.com',
        'http://help.example.com/support',
        'help.example.com',
        'https://example.com/t.me/help',
    ],
)
def test_external_contacts_not_telegram(monkeypatch, contact):
    monkeypatch.setattr(settings, 'SUPPORT_USERNAME', contact, raising=False)
    assert settings.is_support_contact_telegram() is False


@pytest.mark.parametrize('contact', ['', '   '])
def test_empty_contact_is_not_telegram(monkeypatch, contact):
    monkeypatch.setattr(settings, 'SUPPORT_USERNAME', contact, raising=False)
    assert settings.is_support_contact_telegram() is False
