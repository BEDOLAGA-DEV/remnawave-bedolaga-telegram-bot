from datetime import datetime
from types import SimpleNamespace

from app.config import settings
from app.utils.subscription_utils import apply_subscription_domain_override
from app.webapi.routes import subscriptions as subscriptions_route


def test_api_plain_field_overridden(monkeypatch):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    sub = SimpleNamespace(subscription_url='https://old.host/t', subscription_crypto_link='happ://crypt5X')
    field = apply_subscription_domain_override(sub.subscription_url)
    assert field == 'https://cdn.example.com/t'


def _fake_subscription():
    now = datetime(2026, 1, 1)
    return SimpleNamespace(
        id=1,
        user_id=2,
        status='active',
        actual_status='active',
        is_trial=False,
        start_date=now,
        end_date=now,
        traffic_limit_gb=100,
        traffic_used_gb=1.5,
        device_limit=3,
        autopay_enabled=False,
        autopay_days_before=None,
        subscription_url='https://old.host/tok',
        subscription_crypto_link='happ://crypt5OLD',
        connected_squads=['s1'],
        created_at=now,
        updated_at=now,
    )


def test_serializer_applies_override_to_plain_url_only(monkeypatch):
    """Behavioral: the real route serializer overrides the plain url and leaves
    the crypto link untouched. Also catches a missing helper import in the route."""
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    resp = subscriptions_route._serialize_subscription(_fake_subscription())
    assert resp.subscription_url == 'https://cdn.example.com/tok'
    assert resp.subscription_crypto_link == 'happ://crypt5OLD'


def test_serializer_noop_without_override(monkeypatch):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', '', raising=False)
    resp = subscriptions_route._serialize_subscription(_fake_subscription())
    assert resp.subscription_url == 'https://old.host/tok'
