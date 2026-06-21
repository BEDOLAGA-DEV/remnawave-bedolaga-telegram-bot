import pytest

from app.config import settings


@pytest.mark.parametrize(
    'raw,expected',
    [
        ('', None),
        ('   ', None),
        (None, None),
        ('my.com', 'my.com'),
        ('my.com/', 'my.com'),
        ('https://my.com', 'my.com'),
        ('https://my.com/', 'my.com'),
        ('https://my.com/sub/path', 'my.com'),
        ('http://my.com:8443/x', 'my.com:8443'),
        ('  https://My.Com/x  ', 'My.Com'),
    ],
)
def test_get_subscription_domain_override_normalizes(monkeypatch, raw, expected):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', raw, raising=False)
    assert settings.get_subscription_domain_override() == expected


from app.utils.subscription_utils import apply_subscription_domain_override


def test_apply_override_replaces_https_host(monkeypatch):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    out = apply_subscription_domain_override('https://old.host/KC8QUowC')
    assert out == 'https://cdn.example.com/KC8QUowC'


def test_apply_override_replaces_happ_scheme_host(monkeypatch):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    out = apply_subscription_domain_override('happ://old.host/KC8QUowC')
    assert out == 'happ://cdn.example.com/KC8QUowC'


def test_apply_override_preserves_query(monkeypatch):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    out = apply_subscription_domain_override('https://old.host/x?a=1#frag')
    assert out == 'https://cdn.example.com/x?a=1#frag'


def test_apply_override_noop_when_unset(monkeypatch):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', '', raising=False)
    assert apply_subscription_domain_override('https://old.host/x') == 'https://old.host/x'


def test_apply_override_passthrough_empty(monkeypatch):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    assert apply_subscription_domain_override(None) is None
    assert apply_subscription_domain_override('') == ''


def test_apply_override_passthrough_no_netloc(monkeypatch):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    # opaque value with no //netloc — left unchanged
    assert apply_subscription_domain_override('justtoken') == 'justtoken'
