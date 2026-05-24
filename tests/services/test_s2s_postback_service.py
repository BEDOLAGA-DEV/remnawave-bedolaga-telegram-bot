"""Тесты для S2S postback сервиса (SSRF guard, masking, URL substitution)."""

import pytest

from app.services import s2s_postback_service
from app.services.s2s_postback_service import _host_is_private, _is_safe_url, _mask_subid


# ---------- SSRF guard ----------


@pytest.mark.parametrize(
    'host,is_private',
    [
        ('localhost', True),
        ('127.0.0.1', True),
        ('::1', True),
        ('10.0.0.5', True),
        ('172.16.0.1', True),
        ('172.31.255.255', True),
        ('192.168.1.1', True),
        ('169.254.169.254', True),  # AWS IMDS
        ('fc00::1', True),
        ('fe80::1', True),
        ('0.0.0.0', True),
        ('node.internal', True),
        ('foo.local', True),
        ('app.localhost', True),
        ('', True),
        ('partner-tracker.example.com', False),
        ('8.8.8.8', False),
        ('1.1.1.1', False),
        ('keitaro.example.org', False),
    ],
)
def test_host_is_private(host: str, is_private: bool) -> None:
    assert _host_is_private(host) is is_private


@pytest.mark.parametrize(
    'url,is_safe',
    [
        ('https://tracker.example.com/postback?subid=X', True),
        ('http://tracker.example.com/postback', True),
        ('https://1.2.3.4/postback', True),
        ('https://169.254.169.254/latest/meta-data', False),
        ('https://127.0.0.1/postback', False),
        ('https://localhost/postback', False),
        ('https://internal.local/postback', False),
        ('file:///etc/passwd', False),
        ('ftp://tracker.example.com', False),
        ('gopher://1.2.3.4', False),
        ('https://', False),
        ('not-a-url', False),
    ],
)
def test_is_safe_url(url: str, is_safe: bool) -> None:
    assert _is_safe_url(url) is is_safe


# ---------- PII masking ----------


@pytest.mark.parametrize(
    'subid,expected',
    [
        (None, ''),
        ('', ''),
        ('short', 'short'),
        ('abcdefgh', 'abcdefgh'),  # exactly 8 chars — no ellipsis
        ('abcdefghX', 'abcdefgh…'),
        ('keitaro-very-long-tracking-id-12345', 'keitaro-…'),
    ],
)
def test_mask_subid(subid: str | None, expected: str) -> None:
    assert _mask_subid(subid) == expected


# ---------- URL template substitution ----------


@pytest.mark.asyncio
async def test_send_postback_url_substitution_and_encoding(monkeypatch) -> None:
    """Verify {subid}/{event}/{amount}/{user_id}/{tx_id} substitution + URL-encoding."""

    captured: dict = {}

    class _FakeResponse:
        status_code = 200

    class _FakeClient:
        is_closed = False

        async def get(self, url, *args, **kwargs):
            captured['url'] = url
            return _FakeResponse()

    fake_client = _FakeClient()
    monkeypatch.setattr(s2s_postback_service, '_get_client', lambda: fake_client)
    monkeypatch.setattr(s2s_postback_service, '_is_enabled', lambda: True)
    monkeypatch.setattr(
        s2s_postback_service,
        '_get_url',
        lambda event: 'https://t.example/p?sub={subid}&ev={event}&amt={amount}&u={user_id}&tx={tx_id}',
    )

    ok = await s2s_postback_service.send_postback(
        'purchase',
        'sub with spaces&special=chars',
        amount=199.5,
        user_id=42,
        tx_id='gp-123/edge?case',
    )
    assert ok is True
    assert 'sub%20with%20spaces%26special%3Dchars' in captured['url']
    assert 'gp-123%2Fedge%3Fcase' in captured['url']
    assert 'ev=purchase' in captured['url']
    assert 'amt=199.5' in captured['url']
    assert 'u=42' in captured['url']


@pytest.mark.asyncio
async def test_send_postback_rejects_unsafe_url(monkeypatch) -> None:
    """SSRF guard fires after substitution — partner URL pointing at IMDS is blocked."""

    monkeypatch.setattr(s2s_postback_service, '_is_enabled', lambda: True)
    monkeypatch.setattr(
        s2s_postback_service,
        '_get_url',
        lambda event: 'http://169.254.169.254/latest/meta-data?sub={subid}',
    )

    ok = await s2s_postback_service.send_postback('purchase', 'subid-x', amount=1.0)
    assert ok is False


@pytest.mark.asyncio
async def test_send_postback_disabled_returns_false(monkeypatch) -> None:
    monkeypatch.setattr(s2s_postback_service, '_is_enabled', lambda: False)
    assert await s2s_postback_service.send_postback('purchase', 'subid-x') is False


@pytest.mark.asyncio
async def test_send_postback_missing_subid_returns_false(monkeypatch) -> None:
    monkeypatch.setattr(s2s_postback_service, '_is_enabled', lambda: True)
    assert await s2s_postback_service.send_postback('purchase', '') is False
