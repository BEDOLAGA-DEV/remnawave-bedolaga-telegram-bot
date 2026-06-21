from types import SimpleNamespace

from app.config import settings
from app.utils.subscription_utils import get_display_subscription_link


def test_my_subscriptions_uses_display_link(monkeypatch):
    """Regression: detail text must use the overridden display link, not raw URL."""
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'link', raising=False)
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    sub = SimpleNamespace(
        subscription_url='https://old.host/tok',
        subscription_crypto_link=None,
    )
    link = get_display_subscription_link(sub)
    assert link == 'https://cdn.example.com/tok'
    rendered = f'\n🔗 <code>{link}</code>'
    assert 'old.host' not in rendered
    assert 'cdn.example.com' in rendered
