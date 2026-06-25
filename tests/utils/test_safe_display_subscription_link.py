from types import SimpleNamespace

from app.config import settings
from app.utils.subscription_utils import get_safe_display_subscription_link


def _sub(url, crypto):
    return SimpleNamespace(subscription_url=url, subscription_crypto_link=crypto)


def test_cryptolink_mode_empty_crypto_returns_none_not_plain(monkeypatch):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'happ_cryptolink', raising=False)
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    # empty string crypto -> None (never leak the plain override URL)
    assert get_safe_display_subscription_link(_sub('https://old.host/tok', '')) is None
    # None crypto -> None
    assert get_safe_display_subscription_link(_sub('https://old.host/tok', None)) is None


def test_cryptolink_mode_returns_crypto_when_present(monkeypatch):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'happ_cryptolink', raising=False)
    sub = _sub('https://old.host/tok', 'happ://crypt5xxxx')
    assert get_safe_display_subscription_link(sub) == 'happ://crypt5xxxx'


def test_plain_mode_returns_overridden_plain(monkeypatch):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'link', raising=False)
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    sub = _sub('https://old.host/tok', None)
    assert get_safe_display_subscription_link(sub) == 'https://cdn.example.com/tok'


def test_none_subscription():
    assert get_safe_display_subscription_link(None) is None
