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
