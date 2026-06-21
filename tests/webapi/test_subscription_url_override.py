from types import SimpleNamespace

from app.config import settings
from app.utils.subscription_utils import apply_subscription_domain_override


def test_api_plain_field_overridden(monkeypatch):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    sub = SimpleNamespace(subscription_url='https://old.host/t', subscription_crypto_link='happ://crypt5X')
    field = apply_subscription_domain_override(sub.subscription_url)
    assert field == 'https://cdn.example.com/t'
