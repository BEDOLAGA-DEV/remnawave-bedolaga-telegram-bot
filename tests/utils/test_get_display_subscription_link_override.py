from types import SimpleNamespace

from app.config import settings
from app.utils.subscription_utils import get_display_subscription_link


def _sub(url, crypto):
    return SimpleNamespace(subscription_url=url, subscription_crypto_link=crypto)


def test_plain_mode_applies_override(monkeypatch):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'link', raising=False)
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    sub = _sub('https://old.host/tok', 'happ://crypt5xxxx')
    assert get_display_subscription_link(sub) == 'https://cdn.example.com/tok'


def test_crypto_mode_returns_stored_crypto_untouched(monkeypatch):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'happ_cryptolink', raising=False)
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    sub = _sub('https://old.host/tok', 'happ://crypt5xxxx')
    # crypto link is already overridden at storage time — returned as-is
    assert get_display_subscription_link(sub) == 'happ://crypt5xxxx'


def test_none_subscription(monkeypatch):
    assert get_display_subscription_link(None) is None
